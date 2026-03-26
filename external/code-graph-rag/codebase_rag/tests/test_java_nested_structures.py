from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, run_updater


@pytest.fixture
def java_nested_project(temp_repo: Path) -> Path:
    """Create a Java project for testing nested structures."""
    project_path = temp_repo / "java_nested_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_deeply_nested_classes(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test deeply nested class structures."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DeeplyNested.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;
import java.util.function.*;

public class OuterClass {
    private String outerField = "outer";

    // First level nested class
    public class FirstLevelNested {
        private String firstField = "first";

        // Second level nested class
        public class SecondLevelNested {
            private String secondField = "second";

            // Third level nested class
            public class ThirdLevelNested {
                private String thirdField = "third";

                // Fourth level nested class
                public class FourthLevelNested {
                    private String fourthField = "fourth";

                    public void accessAllLevels() {
                        System.out.println(outerField);      // Outer class
                        System.out.println(firstField);     // First level
                        System.out.println(secondField);    // Second level
                        System.out.println(thirdField);     // Third level
                        System.out.println(fourthField);    // Fourth level
                    }

                    // Fifth level nested class
                    public class FifthLevelNested {
                        public void deepAccess() {
                            accessAllLevels();
                            System.out.println("Deep nested access");
                        }
                    }
                }
            }
        }

        // Static nested class at first level
        public static class StaticFirstLevel {
            private String staticField = "static_first";

            // Nested within static class
            public class NestedInStatic {
                public void useStaticField() {
                    System.out.println(staticField);
                }
            }

            // Static within static
            public static class StaticInStatic {
                public void cannotAccessOuter() {
                    // Cannot access outerField here
                    System.out.println("Static in static");
                }
            }
        }
    }

    // Interface nested in class
    public interface NestedInterface {
        void performAction();

        // Nested class within interface
        public static class InterfaceNestedClass {
            public void implement() {
                System.out.println("Class nested in interface");
            }
        }

        // Nested interface within interface
        public interface NestedInInterface {
            void deepAction();
        }
    }

    // Enum nested in class
    public enum NestedEnum {
        VALUE1("value1"), VALUE2("value2"), VALUE3("value3");

        private final String value;

        NestedEnum(String value) {
            this.value = value;
        }

        // Method in nested enum
        public String getValue() {
            return value;
        }

        // Class nested in enum
        public static class EnumNestedClass {
            public void processEnumValue(NestedEnum enumValue) {
                System.out.println("Processing: " + enumValue.getValue());
            }
        }
    }

    // Method to create nested instances
    public void demonstrateNesting() {
        // Create nested instances
        FirstLevelNested first = new FirstLevelNested();
        FirstLevelNested.SecondLevelNested second = first.new SecondLevelNested();
        FirstLevelNested.SecondLevelNested.ThirdLevelNested third = second.new ThirdLevelNested();
        FirstLevelNested.SecondLevelNested.ThirdLevelNested.FourthLevelNested fourth = third.new FourthLevelNested();
        FirstLevelNested.SecondLevelNested.ThirdLevelNested.FourthLevelNested.FifthLevelNested fifth = fourth.new FifthLevelNested();

        fifth.deepAccess();

        // Static nested class
        FirstLevelNested.StaticFirstLevel staticFirst = new FirstLevelNested.StaticFirstLevel();
        FirstLevelNested.StaticFirstLevel.NestedInStatic nestedInStatic = staticFirst.new NestedInStatic();
        FirstLevelNested.StaticFirstLevel.StaticInStatic staticInStatic = new FirstLevelNested.StaticFirstLevel.StaticInStatic();

        // Interface and enum usage
        NestedInterface.InterfaceNestedClass interfaceNested = new NestedInterface.InterfaceNestedClass();
        NestedEnum.EnumNestedClass enumNested = new NestedEnum.EnumNestedClass();
        enumNested.processEnumValue(NestedEnum.VALUE1);
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_outer_class = {
        f"{project_name}.src.main.java.com.example.DeeplyNested.OuterClass",
    }

    missing_classes = expected_outer_class - created_classes
    assert not missing_classes, (
        f"Missing expected outer class: {sorted(list(missing_classes))}"
    )


def test_anonymous_classes_complex(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex anonymous class patterns."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AnonymousComplex.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.concurrent.*;

public class AnonymousComplex {
    private final String instanceField = "instance";
    private static final String STATIC_FIELD = "static";

    // Anonymous class implementing interface
    public void anonymousInterface() {
        final String localVar = "local";

        Runnable task = new Runnable() {
            private String anonymousField = "anonymous";

            @Override
            public void run() {
                System.out.println(instanceField);  // Access outer instance
                System.out.println(STATIC_FIELD);   // Access static
                System.out.println(localVar);       // Access local final
                System.out.println(anonymousField); // Access anonymous field

                // Anonymous class within anonymous class
                SwingUtilities.invokeLater(new Runnable() {
                    @Override
                    public void run() {
                        System.out.println("Nested anonymous: " + anonymousField);
                    }
                });
            }

            // Additional method in anonymous class
            public void customMethod() {
                System.out.println("Custom method in anonymous class");
            }
        };

        task.run();

        // Cast to access custom method
        if (task instanceof Runnable) {
            // ((AnonymousRunnableSubclass) task).customMethod(); // Not accessible
        }
    }

    // Anonymous class extending abstract class
    public abstract static class AbstractProcessor {
        protected String name;

        public AbstractProcessor(String name) {
            this.name = name;
        }

        public abstract void process(String data);

        public void preProcess() {
            System.out.println("Pre-processing with " + name);
        }
    }

    public void anonymousAbstractClass() {
        final int processingId = 123;

        AbstractProcessor processor = new AbstractProcessor("Anonymous Processor") {
            private List<String> processedItems = new ArrayList<>();

            @Override
            public void process(String data) {
                preProcess(); // Call inherited method

                String processed = "Processed[" + processingId + "]: " + data;
                processedItems.add(processed);

                System.out.println(processed);
                System.out.println("Total processed: " + processedItems.size());
            }

            // Additional methods
            public List<String> getProcessedItems() {
                return new ArrayList<>(processedItems);
            }

            // Override toString
            @Override
            public String toString() {
                return name + " (" + processedItems.size() + " items)";
            }
        };

        processor.process("test data");
        System.out.println(processor);
    }

    // Anonymous class with generics
    public void anonymousWithGenerics() {
        // Anonymous Comparator
        Comparator<String> lengthComparator = new Comparator<String>() {
            @Override
            public int compare(String s1, String s2) {
                int lengthDiff = s1.length() - s2.length();
                return lengthDiff != 0 ? lengthDiff : s1.compareTo(s2);
            }
        };

        // Anonymous Function
        Function<String, Integer> stringProcessor = new Function<String, Integer>() {
            private Map<String, Integer> cache = new HashMap<>();

            @Override
            public Integer apply(String input) {
                return cache.computeIfAbsent(input, s -> {
                    // Nested lambda within anonymous class
                    return s.chars().map(Character::toUpperCase).sum();
                });
            }

            public void clearCache() {
                cache.clear();
            }
        };

        // Anonymous BiFunction with complex logic
        BiFunction<List<String>, Predicate<String>, Map<String, Long>> groupCounter =
            new BiFunction<List<String>, Predicate<String>, Map<String, Long>>() {

                @Override
                public Map<String, Long> apply(List<String> items, Predicate<String> filter) {
                    return items.stream()
                        .filter(filter)
                        .collect(groupingBy(
                            s -> s.substring(0, Math.min(1, s.length())),
                            Collectors.counting()
                        ));
                }
            };

        // Usage
        List<String> items = Arrays.asList("apple", "banana", "apricot", "blueberry");
        Map<String, Long> grouped = groupCounter.apply(items, s -> s.length() > 4);
    }

    // Anonymous class in collection operations
    public void anonymousInCollections() {
        List<String> names = Arrays.asList("Alice", "Bob", "Charlie", "David");

        // Anonymous iterator
        Iterator<String> customIterator = new Iterator<String>() {
            private int index = 0;
            private boolean reverse = false;

            @Override
            public boolean hasNext() {
                return reverse ? index > 0 : index < names.size();
            }

            @Override
            public String next() {
                if (!hasNext()) {
                    throw new NoSuchElementException();
                }

                String result = names.get(reverse ? --index : index++);

                // Switch direction when reaching end
                if (!hasNext()) {
                    reverse = !reverse;
                    if (reverse) {
                        index = names.size();
                    } else {
                        index = 0;
                    }
                }

                return result;
            }
        };

        // Anonymous Map implementation
        Map<String, String> customMap = new HashMap<String, String>() {
            @Override
            public String put(String key, String value) {
                System.out.println("Putting: " + key + " -> " + value);
                return super.put(key.toUpperCase(), value.toLowerCase());
            }

            @Override
            public String get(Object key) {
                String result = super.get(key.toString().toUpperCase());
                System.out.println("Getting: " + key + " -> " + result);
                return result;
            }
        };

        customMap.put("Hello", "WORLD");
        String value = customMap.get("hello");
    }

    // Anonymous class in event handling pattern
    public interface EventListener<T> {
        void onEvent(T event);

        default void onError(Exception e) {
            System.err.println("Error: " + e.getMessage());
        }
    }

    public static class Event {
        private final String type;
        private final Object data;
        private final long timestamp;

        public Event(String type, Object data) {
            this.type = type;
            this.data = data;
            this.timestamp = System.currentTimeMillis();
        }

        public String getType() { return type; }
        public Object getData() { return data; }
        public long getTimestamp() { return timestamp; }
    }

    public void anonymousEventHandling() {
        final AtomicInteger eventCount = new AtomicInteger(0);

        EventListener<Event> complexListener = new EventListener<Event>() {
            private Map<String, Integer> eventTypeCounts = new HashMap<>();
            private List<Event> recentEvents = new ArrayList<>();

            @Override
            public void onEvent(Event event) {
                eventCount.incrementAndGet();

                // Update type counts
                eventTypeCounts.merge(event.getType(), 1, Integer::sum);

                // Keep only recent events
                recentEvents.add(event);
                if (recentEvents.size() > 10) {
                    recentEvents.remove(0);
                }

                // Complex processing based on event type
                switch (event.getType()) {
                    case "USER_LOGIN" -> handleUserLogin(event);
                    case "DATA_UPDATE" -> handleDataUpdate(event);
                    case "SYSTEM_ERROR" -> handleSystemError(event);
                    default -> handleGenericEvent(event);
                }

                // Nested anonymous class for async processing
                CompletableFuture.runAsync(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            Thread.sleep(100); // Simulate processing
                            System.out.println("Async processed: " + event.getType());
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                            onError(e);
                        }
                    }
                });
            }

            private void handleUserLogin(Event event) {
                System.out.println("User login processed");
            }

            private void handleDataUpdate(Event event) {
                System.out.println("Data update processed");
            }

            private void handleSystemError(Event event) {
                System.err.println("System error: " + event.getData());
            }

            private void handleGenericEvent(Event event) {
                System.out.println("Generic event: " + event.getType());
            }

            @Override
            public void onError(Exception e) {
                EventListener.super.onError(e);
                System.err.println("Event processing failed for event count: " + eventCount.get());
            }
        };

        // Test events
        complexListener.onEvent(new Event("USER_LOGIN", "user123"));
        complexListener.onEvent(new Event("DATA_UPDATE", "dataset1"));
        complexListener.onEvent(new Event("SYSTEM_ERROR", "Out of memory"));
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.AnonymousComplex.AnonymousComplex",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_local_classes_in_methods(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test local classes defined within methods."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "LocalClasses.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;
import java.util.function.*;

public class LocalClasses {
    private String instanceField = "instance";

    // Method with simple local class
    public void methodWithLocalClass() {
        final String methodLocal = "method_local";
        int methodVar = 42;

        // Local class definition
        class LocalCalculator {
            private int localField = 100;

            public int calculate() {
                // Can access final/effectively final variables
                return localField + methodVar + methodLocal.length();
            }

            public void printInfo() {
                System.out.println("Instance field: " + instanceField);
                System.out.println("Method local: " + methodLocal);
                System.out.println("Method var: " + methodVar);
                System.out.println("Local field: " + localField);
            }
        }

        // Use local class
        LocalCalculator calculator = new LocalCalculator();
        int result = calculator.calculate();
        calculator.printInfo();

        System.out.println("Calculation result: " + result);
    }

    // Method with local class implementing interface
    public Comparator<String> createCustomComparator(boolean caseSensitive) {
        final String prefix = caseSensitive ? "CS_" : "CI_";

        class CustomComparator implements Comparator<String> {
            private int comparisonCount = 0;

            @Override
            public int compare(String s1, String s2) {
                comparisonCount++;
                System.out.println(prefix + "Comparison #" + comparisonCount);

                if (caseSensitive) {
                    return s1.compareTo(s2);
                } else {
                    return s1.compareToIgnoreCase(s2);
                }
            }

            public int getComparisonCount() {
                return comparisonCount;
            }

            @Override
            public String toString() {
                return prefix + "Comparator (comparisons: " + comparisonCount + ")";
            }
        }

        return new CustomComparator();
    }

    // Method with multiple local classes
    public void multipleLocalClasses() {
        final List<String> sharedData = new ArrayList<>();

        // First local class
        class DataProducer {
            private String producerName;

            public DataProducer(String name) {
                this.producerName = name;
            }

            public void produce() {
                for (int i = 0; i < 5; i++) {
                    String data = producerName + "_item_" + i;
                    sharedData.add(data);
                    System.out.println("Produced: " + data);
                }
            }
        }

        // Second local class
        class DataConsumer {
            private String consumerName;
            private List<String> consumedItems = new ArrayList<>();

            public DataConsumer(String name) {
                this.consumerName = name;
            }

            public void consume() {
                while (!sharedData.isEmpty()) {
                    String item = sharedData.remove(0);
                    consumedItems.add(item);
                    System.out.println(consumerName + " consumed: " + item);
                }
            }

            public List<String> getConsumedItems() {
                return new ArrayList<>(consumedItems);
            }
        }

        // Third local class for processing
        class DataProcessor {
            public void process(List<String> items) {
                System.out.println("Processing " + items.size() + " items:");
                items.forEach(item -> System.out.println("  - " + item.toUpperCase()));
            }
        }

        // Use local classes
        DataProducer producer = new DataProducer("Producer1");
        DataConsumer consumer = new DataConsumer("Consumer1");
        DataProcessor processor = new DataProcessor();

        producer.produce();
        consumer.consume();
        processor.process(consumer.getConsumedItems());
    }

    // Local class with generics
    public <T> Function<T, String> createToStringFunction(String format) {
        final String template = format != null ? format : "Value: %s";

        class ToStringConverter<U> implements Function<U, String> {
            private int conversionCount = 0;

            @Override
            public String apply(U input) {
                conversionCount++;
                if (input == null) {
                    return template.replace("%s", "null");
                }
                return template.replace("%s", input.toString()) + " (#" + conversionCount + ")";
            }

            public int getConversionCount() {
                return conversionCount;
            }
        }

        @SuppressWarnings("unchecked")
        Function<T, String> converter = (Function<T, String>) new ToStringConverter<T>();
        return converter;
    }

    // Nested method with local class
    public void nestedMethodsWithLocalClasses() {
        final String outerMethodVar = "outer_method";

        class OuterLocalClass {
            public void methodInLocalClass() {
                final String innerMethodVar = "inner_method";

                // Local class within method of local class
                class InnerLocalClass {
                    public void deepMethod() {
                        System.out.println("Instance: " + instanceField);
                        System.out.println("Outer method: " + outerMethodVar);
                        System.out.println("Inner method: " + innerMethodVar);

                        // Anonymous class within local class within local class
                        Runnable task = new Runnable() {
                            @Override
                            public void run() {
                                System.out.println("Anonymous in local in local: " +
                                    instanceField + ", " + outerMethodVar + ", " + innerMethodVar);
                            }
                        };

                        task.run();
                    }
                }

                InnerLocalClass inner = new InnerLocalClass();
                inner.deepMethod();
            }
        }

        OuterLocalClass outer = new OuterLocalClass();
        outer.methodInLocalClass();
    }

    // Local class in static method
    public static void staticMethodWithLocalClass() {
        final String staticMethodVar = "static_method";

        class StaticLocalClass {
            private String localField = "static_local";

            public void showAccess() {
                // Can access static method variables
                System.out.println("Static method var: " + staticMethodVar);
                System.out.println("Local field: " + localField);

                // Cannot access instance fields
                // System.out.println("Instance field: " + instanceField); // COMPILE ERROR
            }

            // Can have static members in local class (Java 16+)
            public static void staticMethodInLocalClass() {
                System.out.println("Static method in local class");
            }
        }

        StaticLocalClass localInstance = new StaticLocalClass();
        localInstance.showAccess();
        StaticLocalClass.staticMethodInLocalClass();
    }

    // Local class in constructor
    public LocalClasses(String initValue) {
        final String constructorVar = "constructor_" + initValue;

        class ConstructorLocalClass {
            public void initialize() {
                instanceField = constructorVar + "_initialized";
                System.out.println("Initialized with: " + instanceField);
            }
        }

        ConstructorLocalClass initializer = new ConstructorLocalClass();
        initializer.initialize();
    }

    // Default constructor
    public LocalClasses() {
        this("default");
    }

    // Local class in lambda context
    public void localClassInLambdaContext() {
        final List<String> items = Arrays.asList("apple", "banana", "cherry");

        items.forEach(item -> {
            final String processedItem = item.toUpperCase();

            // Local class within lambda
            class LambdaLocalProcessor {
                public String process() {
                    return "Processed: " + processedItem + " (length: " + processedItem.length() + ")";
                }
            }

            LambdaLocalProcessor processor = new LambdaLocalProcessor();
            System.out.println(processor.process());
        });
    }

    // Local class accessing method parameters
    public String processData(String data, Function<String, String> transformer) {
        final String methodParam = data;
        final Function<String, String> methodTransformer = transformer;

        class ParameterProcessor {
            public String process() {
                String transformed = methodTransformer.apply(methodParam);
                return "Parameter processed: " + transformed;
            }
        }

        ParameterProcessor processor = new ParameterProcessor();
        return processor.process();
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.LocalClasses.LocalClasses",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_lambda_edge_cases(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex lambda expression edge cases."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "LambdaEdgeCases.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.concurrent.*;

public class LambdaEdgeCases {

    // Lambda capturing different types of variables
    public void variableCapture() {
        final String finalVar = "final";
        String effectivelyFinal = "effectively_final";

        // Don't modify effectivelyFinal after this point

        AtomicInteger counter = new AtomicInteger(0);
        List<String> mutableList = new ArrayList<>();

        // Lambda capturing final variable
        Supplier<String> finalCapture = () -> "Final: " + finalVar;

        // Lambda capturing effectively final variable
        Supplier<String> effectivelyFinalCapture = () -> "Effectively final: " + effectivelyFinal;

        // Lambda capturing mutable object (reference is final)
        Consumer<String> listAdder = item -> {
            mutableList.add(item);
            counter.incrementAndGet();
        };

        // Lambda with complex capture
        Function<String, String> complexCapture = input -> {
            return finalVar + "_" + effectivelyFinal + "_" + input + "_" + counter.get();
        };

        // Use lambdas
        listAdder.accept("item1");
        listAdder.accept("item2");

        System.out.println(finalCapture.get());
        System.out.println(effectivelyFinalCapture.get());
        System.out.println(complexCapture.apply("test"));
        System.out.println("List contents: " + mutableList);
    }

    // Recursive lambda expressions
    public void recursiveLambdas() {
        // Recursive lambda using method reference to itself
        Function<Integer, Integer> factorial = new Function<Integer, Integer>() {
            @Override
            public Integer apply(Integer n) {
                return n <= 1 ? 1 : n * this.apply(n - 1);
            }
        };

        System.out.println("Factorial 5: " + factorial.apply(5));

        // Recursive lambda with explicit self-reference
        class RecursiveLambdaHolder {
            Function<Integer, Integer> fibonacci = n -> {
                if (n <= 1) return n;
                return this.fibonacci.apply(n - 1) + this.fibonacci.apply(n - 2);
            };
        }

        RecursiveLambdaHolder holder = new RecursiveLambdaHolder();
        System.out.println("Fibonacci 10: " + holder.fibonacci.apply(10));

        // Mutual recursion with lambdas
        class MutualRecursion {
            Function<Integer, Boolean> isEven = n -> n == 0 || isOdd.apply(n - 1);
            Function<Integer, Boolean> isOdd = n -> n != 0 && isEven.apply(n - 1);
        }

        MutualRecursion mr = new MutualRecursion();
        System.out.println("Is 4 even: " + mr.isEven.apply(4));
        System.out.println("Is 5 odd: " + mr.isOdd.apply(5));
    }

    // Lambda expressions with exception handling
    public void lambdaExceptionHandling() {
        // Functional interface that throws checked exception
        @FunctionalInterface
        interface ThrowingFunction<T, R, E extends Exception> {
            R apply(T t) throws E;
        }

        // Wrapper to convert throwing function to regular function
        Function<String, Integer> wrappedFunction = input -> {
            try {
                ThrowingFunction<String, Integer, NumberFormatException> parser = Integer::parseInt;
                return parser.apply(input);
            } catch (NumberFormatException e) {
                System.err.println("Parse error: " + e.getMessage());
                return 0;
            }
        };

        // Lambda with try-catch
        Consumer<String> safeProcessor = input -> {
            try {
                int value = Integer.parseInt(input);
                System.out.println("Processed value: " + value * 2);
            } catch (NumberFormatException e) {
                System.err.println("Invalid input: " + input);
            } catch (Exception e) {
                System.err.println("Unexpected error: " + e.getMessage());
            }
        };

        // Lambda that rethrows as runtime exception
        Function<String, Integer> rethrowing = input -> {
            try {
                return Integer.parseInt(input);
            } catch (NumberFormatException e) {
                throw new RuntimeException("Failed to parse: " + input, e);
            }
        };

        // Test exception handling
        List<String> inputs = Arrays.asList("123", "invalid", "456", "bad");
        inputs.forEach(safeProcessor);

        inputs.stream()
            .map(wrappedFunction)
            .forEach(result -> System.out.println("Result: " + result));
    }

    // Complex lambda chaining and composition
    public void lambdaComposition() {
        // Function composition
        Function<String, String> trim = String::trim;
        Function<String, String> upper = String::toUpperCase;
        Function<String, Integer> length = String::length;
        Function<Integer, String> format = i -> String.format("[%d chars]", i);

        Function<String, String> pipeline = trim
            .andThen(upper)
            .andThen(length)
            .andThen(format);

        // Predicate composition
        Predicate<String> notNull = Objects::nonNull;
        Predicate<String> notEmpty = s -> !s.isEmpty();
        Predicate<String> hasContent = notNull.and(notEmpty);
        Predicate<String> isLong = s -> s.length() > 5;
        Predicate<String> isValidAndLong = hasContent.and(isLong);

        // Consumer composition
        Consumer<String> printer = System.out::println;
        Consumer<String> logger = s -> System.err.println("LOG: " + s);
        Consumer<String> combined = printer.andThen(logger);

        // Complex composition example
        List<String> inputs = Arrays.asList("  hello world  ", "", null, "short", "  this is a long string  ");

        inputs.stream()
            .filter(hasContent)
            .map(pipeline)
            .forEach(combined);

        // Lambda returning lambda
        Function<String, Function<String, String>> prefixAdder = prefix ->
            text -> prefix + ": " + text;

        Function<String, String> errorPrefixer = prefixAdder.apply("ERROR");
        Function<String, String> infoPrefixer = prefixAdder.apply("INFO");

        System.out.println(errorPrefixer.apply("Something went wrong"));
        System.out.println(infoPrefixer.apply("Everything is fine"));
    }

    // Lambda expressions with generics and wildcards
    public void lambdaGenerics() {
        // Generic lambda with bounds
        Function<List<? extends Number>, Double> averageCalculator = numbers -> {
            return numbers.stream()
                .mapToDouble(Number::doubleValue)
                .average()
                .orElse(0.0);
        };

        // Lambda with wildcard consumers
        Consumer<List<? super String>> listPopulator = list -> {
            list.add("Lambda");
            list.add("Generics");
            list.add("Example");
        };

        // Bi-function with complex generics
        BiFunction<Map<String, ? extends Number>, String, Optional<Double>> mapValueExtractor =
            (map, key) -> {
                Number value = map.get(key);
                return value != null ? Optional.of(value.doubleValue()) : Optional.empty();
            };

        // Generic method reference with type inference
        Function<String[], List<String>> arrayToList = Arrays::asList;
        Function<Collection<String>, String[]> collectionToArray =
            coll -> coll.toArray(new String[0]);

        // Test generic lambdas
        List<Integer> integers = Arrays.asList(1, 2, 3, 4, 5);
        List<Double> doubles = Arrays.asList(1.1, 2.2, 3.3, 4.4, 5.5);

        System.out.println("Integer average: " + averageCalculator.apply(integers));
        System.out.println("Double average: " + averageCalculator.apply(doubles));

        List<Object> objectList = new ArrayList<>();
        listPopulator.accept(objectList);
        System.out.println("Populated list: " + objectList);

        Map<String, Integer> intMap = Map.of("a", 1, "b", 2, "c", 3);
        Optional<Double> extracted = mapValueExtractor.apply(intMap, "b");
        System.out.println("Extracted value: " + extracted);
    }

    // Lambda expressions in parallel processing
    public void parallelLambdas() {
        List<Integer> numbers = IntStream.rangeClosed(1, 1000).boxed().toList();

        // Sequential processing
        long sequentialSum = numbers.stream()
            .mapToLong(n -> {
                // Simulate some work
                try {
                    Thread.sleep(1);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return n * n;
            })
            .sum();

        // Parallel processing
        long parallelSum = numbers.parallelStream()
            .mapToLong(n -> {
                // Same work in parallel
                try {
                    Thread.sleep(1);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return n * n;
            })
            .sum();

        System.out.println("Sequential sum: " + sequentialSum);
        System.out.println("Parallel sum: " + parallelSum);

        // Parallel reduction with lambda
        String concatenated = numbers.parallelStream()
            .limit(10)
            .map(Object::toString)
            .reduce("", (a, b) -> a + "," + b, (a, b) -> a + b);

        System.out.println("Concatenated: " + concatenated);

        // CompletableFuture with lambdas
        List<CompletableFuture<String>> futures = numbers.stream()
            .limit(5)
            .map(n -> CompletableFuture.supplyAsync(() -> {
                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return "Processed: " + n;
            }))
            .toList();

        CompletableFuture<Void> allOf = CompletableFuture.allOf(
            futures.toArray(new CompletableFuture[0])
        );

        allOf.thenRun(() -> {
            System.out.println("All futures completed");
            futures.forEach(future -> {
                try {
                    System.out.println(future.get());
                } catch (Exception e) {
                    System.err.println("Future failed: " + e.getMessage());
                }
            });
        });
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.LambdaEdgeCases.LambdaEdgeCases",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_builder_pattern_nested(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test nested classes in builder pattern implementation."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BuilderPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;
import java.time.LocalDateTime;

// Complex builder pattern with nested classes
public class ComplexObject {
    private final String name;
    private final int value;
    private final List<String> tags;
    private final Configuration configuration;
    private final Map<String, Object> metadata;
    private final LocalDateTime createdAt;

    // Private constructor - only accessible via builder
    private ComplexObject(Builder builder) {
        this.name = builder.name;
        this.value = builder.value;
        this.tags = List.copyOf(builder.tags);
        this.configuration = builder.configuration;
        this.metadata = Map.copyOf(builder.metadata);
        this.createdAt = LocalDateTime.now();
    }

    // Getters
    public String getName() { return name; }
    public int getValue() { return value; }
    public List<String> getTags() { return tags; }
    public Configuration getConfiguration() { return configuration; }
    public Map<String, Object> getMetadata() { return metadata; }
    public LocalDateTime getCreatedAt() { return createdAt; }

    // Static method to start building
    public static Builder builder() {
        return new Builder();
    }

    public static Builder builder(String name) {
        return new Builder().name(name);
    }

    // Configuration nested class
    public static class Configuration {
        private final boolean enabled;
        private final int timeout;
        private final String mode;
        private final Properties properties;

        private Configuration(ConfigurationBuilder builder) {
            this.enabled = builder.enabled;
            this.timeout = builder.timeout;
            this.mode = builder.mode;
            this.properties = new Properties();
            this.properties.putAll(builder.properties);
        }

        public boolean isEnabled() { return enabled; }
        public int getTimeout() { return timeout; }
        public String getMode() { return mode; }
        public Properties getProperties() { return properties; }

        public static ConfigurationBuilder builder() {
            return new ConfigurationBuilder();
        }

        // Nested builder for Configuration
        public static class ConfigurationBuilder {
            private boolean enabled = true;
            private int timeout = 30;
            private String mode = "default";
            private Properties properties = new Properties();

            public ConfigurationBuilder enabled(boolean enabled) {
                this.enabled = enabled;
                return this;
            }

            public ConfigurationBuilder timeout(int timeout) {
                if (timeout <= 0) {
                    throw new IllegalArgumentException("Timeout must be positive");
                }
                this.timeout = timeout;
                return this;
            }

            public ConfigurationBuilder mode(String mode) {
                this.mode = Objects.requireNonNull(mode, "Mode cannot be null");
                return this;
            }

            public ConfigurationBuilder property(String key, String value) {
                this.properties.setProperty(key, value);
                return this;
            }

            public ConfigurationBuilder properties(Properties properties) {
                this.properties.clear();
                this.properties.putAll(properties);
                return this;
            }

            // Validation method
            public ConfigurationBuilder validate() {
                if (timeout <= 0) {
                    throw new IllegalStateException("Invalid timeout: " + timeout);
                }
                if (mode == null || mode.trim().isEmpty()) {
                    throw new IllegalStateException("Mode cannot be empty");
                }
                return this;
            }

            public Configuration build() {
                validate();
                return new Configuration(this);
            }
        }
    }

    // Main builder class
    public static class Builder {
        private String name;
        private int value;
        private List<String> tags = new ArrayList<>();
        private Configuration configuration;
        private Map<String, Object> metadata = new HashMap<>();

        private Builder() {}

        public Builder name(String name) {
            this.name = Objects.requireNonNull(name, "Name cannot be null");
            return this;
        }

        public Builder value(int value) {
            this.value = value;
            return this;
        }

        public Builder tag(String tag) {
            if (tag != null && !tag.trim().isEmpty()) {
                this.tags.add(tag.trim());
            }
            return this;
        }

        public Builder tags(String... tags) {
            for (String tag : tags) {
                tag(tag);
            }
            return this;
        }

        public Builder tags(Collection<String> tags) {
            tags.forEach(this::tag);
            return this;
        }

        public Builder configuration(Configuration configuration) {
            this.configuration = configuration;
            return this;
        }

        public Builder configuration(Configuration.ConfigurationBuilder configBuilder) {
            this.configuration = configBuilder.build();
            return this;
        }

        // Fluent configuration building
        public ConfigurationStep configureWith() {
            return new ConfigurationStep(this);
        }

        public Builder metadata(String key, Object value) {
            this.metadata.put(key, value);
            return this;
        }

        public Builder metadata(Map<String, Object> metadata) {
            this.metadata.putAll(metadata);
            return this;
        }

        // Validation
        public Builder validate() {
            if (name == null || name.trim().isEmpty()) {
                throw new IllegalStateException("Name is required");
            }
            if (value < 0) {
                throw new IllegalStateException("Value must be non-negative");
            }
            return this;
        }

        // Build method
        public ComplexObject build() {
            validate();

            // Set default configuration if not provided
            if (configuration == null) {
                configuration = Configuration.builder().build();
            }

            return new ComplexObject(this);
        }

        // Conditional building
        public Builder when(boolean condition, Consumer<Builder> action) {
            if (condition) {
                action.accept(this);
            }
            return this;
        }

        // Copy from existing object
        public Builder from(ComplexObject other) {
            this.name = other.name;
            this.value = other.value;
            this.tags.clear();
            this.tags.addAll(other.tags);
            this.configuration = other.configuration;
            this.metadata.clear();
            this.metadata.putAll(other.metadata);
            return this;
        }
    }

    // Helper class for fluent configuration
    public static class ConfigurationStep {
        private final Builder builder;
        private final Configuration.ConfigurationBuilder configBuilder;

        private ConfigurationStep(Builder builder) {
            this.builder = builder;
            this.configBuilder = Configuration.builder();
        }

        public ConfigurationStep enabled(boolean enabled) {
            configBuilder.enabled(enabled);
            return this;
        }

        public ConfigurationStep timeout(int timeout) {
            configBuilder.timeout(timeout);
            return this;
        }

        public ConfigurationStep mode(String mode) {
            configBuilder.mode(mode);
            return this;
        }

        public ConfigurationStep property(String key, String value) {
            configBuilder.property(key, value);
            return this;
        }

        public Builder done() {
            builder.configuration(configBuilder.build());
            return builder;
        }
    }

    // Factory methods using builders
    public static ComplexObject createDefault() {
        return builder()
            .name("default")
            .value(0)
            .build();
    }

    public static ComplexObject createWithConfiguration(String name, int value, boolean enabled) {
        return builder()
            .name(name)
            .value(value)
            .configureWith()
                .enabled(enabled)
                .timeout(60)
                .mode("production")
                .property("debug", "false")
                .done()
            .build();
    }

    @Override
    public String toString() {
        return "ComplexObject{" +
            "name='" + name + '\'' +
            ", value=" + value +
            ", tags=" + tags +
            ", configuration=" + configuration +
            ", metadata=" + metadata +
            ", createdAt=" + createdAt +
            '}';
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        ComplexObject that = (ComplexObject) o;
        return value == that.value &&
            Objects.equals(name, that.name) &&
            Objects.equals(tags, that.tags) &&
            Objects.equals(configuration, that.configuration) &&
            Objects.equals(metadata, that.metadata);
    }

    @Override
    public int hashCode() {
        return Objects.hash(name, value, tags, configuration, metadata);
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.BuilderPattern.ComplexObject",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_visitor_pattern_nested(
    java_nested_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test nested classes in visitor pattern implementation."""
    test_file = (
        java_nested_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "VisitorPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.*;

// Expression tree with visitor pattern using nested classes
public abstract class Expression {

    // Visitor interface
    public interface Visitor<T> {
        T visitNumber(NumberExpression expr);
        T visitBinary(BinaryExpression expr);
        T visitUnary(UnaryExpression expr);
        T visitVariable(VariableExpression expr);
        T visitFunction(FunctionExpression expr);
    }

    // Accept method for visitor pattern
    public abstract <T> T accept(Visitor<T> visitor);

    // Number expression
    public static class NumberExpression extends Expression {
        private final double value;

        public NumberExpression(double value) {
            this.value = value;
        }

        public double getValue() {
            return value;
        }

        @Override
        public <T> T accept(Visitor<T> visitor) {
            return visitor.visitNumber(this);
        }

        @Override
        public String toString() {
            return String.valueOf(value);
        }
    }

    // Binary operation expression
    public static class BinaryExpression extends Expression {
        private final Expression left;
        private final Expression right;
        private final BinaryOperator operator;

        public BinaryExpression(Expression left, BinaryOperator operator, Expression right) {
            this.left = Objects.requireNonNull(left);
            this.right = Objects.requireNonNull(right);
            this.operator = Objects.requireNonNull(operator);
        }

        public Expression getLeft() { return left; }
        public Expression getRight() { return right; }
        public BinaryOperator getOperator() { return operator; }

        @Override
        public <T> T accept(Visitor<T> visitor) {
            return visitor.visitBinary(this);
        }

        @Override
        public String toString() {
            return "(" + left + " " + operator.getSymbol() + " " + right + ")";
        }

        // Binary operators enum
        public enum BinaryOperator {
            ADD("+"), SUBTRACT("-"), MULTIPLY("*"), DIVIDE("/"), POWER("^");

            private final String symbol;

            BinaryOperator(String symbol) {
                this.symbol = symbol;
            }

            public String getSymbol() {
                return symbol;
            }
        }
    }

    // Unary operation expression
    public static class UnaryExpression extends Expression {
        private final Expression operand;
        private final UnaryOperator operator;

        public UnaryExpression(UnaryOperator operator, Expression operand) {
            this.operator = Objects.requireNonNull(operator);
            this.operand = Objects.requireNonNull(operand);
        }

        public Expression getOperand() { return operand; }
        public UnaryOperator getOperator() { return operator; }

        @Override
        public <T> T accept(Visitor<T> visitor) {
            return visitor.visitUnary(this);
        }

        @Override
        public String toString() {
            return operator.getSymbol() + "(" + operand + ")";
        }

        // Unary operators enum
        public enum UnaryOperator {
            NEGATE("-"), ABSOLUTE("abs"), SINE("sin"), COSINE("cos"), LOGARITHM("log");

            private final String symbol;

            UnaryOperator(String symbol) {
                this.symbol = symbol;
            }

            public String getSymbol() {
                return symbol;
            }
        }
    }

    // Variable expression
    public static class VariableExpression extends Expression {
        private final String name;

        public VariableExpression(String name) {
            this.name = Objects.requireNonNull(name);
        }

        public String getName() {
            return name;
        }

        @Override
        public <T> T accept(Visitor<T> visitor) {
            return visitor.visitVariable(this);
        }

        @Override
        public String toString() {
            return name;
        }
    }

    // Function expression
    public static class FunctionExpression extends Expression {
        private final String functionName;
        private final List<Expression> arguments;

        public FunctionExpression(String functionName, List<Expression> arguments) {
            this.functionName = Objects.requireNonNull(functionName);
            this.arguments = List.copyOf(arguments);
        }

        public String getFunctionName() { return functionName; }
        public List<Expression> getArguments() { return arguments; }

        @Override
        public <T> T accept(Visitor<T> visitor) {
            return visitor.visitFunction(this);
        }

        @Override
        public String toString() {
            return functionName + "(" + String.join(", ",
                arguments.stream().map(Object::toString).toList()) + ")";
        }
    }

    // Concrete visitors as nested classes

    // Evaluation visitor
    public static class EvaluationVisitor implements Visitor<Double> {
        private final Map<String, Double> variables;

        public EvaluationVisitor() {
            this(new HashMap<>());
        }

        public EvaluationVisitor(Map<String, Double> variables) {
            this.variables = new HashMap<>(variables);
        }

        public void setVariable(String name, double value) {
            variables.put(name, value);
        }

        @Override
        public Double visitNumber(NumberExpression expr) {
            return expr.getValue();
        }

        @Override
        public Double visitBinary(BinaryExpression expr) {
            double left = expr.getLeft().accept(this);
            double right = expr.getRight().accept(this);

            return switch (expr.getOperator()) {
                case ADD -> left + right;
                case SUBTRACT -> left - right;
                case MULTIPLY -> left * right;
                case DIVIDE -> {
                    if (right == 0) {
                        throw new ArithmeticException("Division by zero");
                    }
                    yield left / right;
                }
                case POWER -> Math.pow(left, right);
            };
        }

        @Override
        public Double visitUnary(UnaryExpression expr) {
            double operand = expr.getOperand().accept(this);

            return switch (expr.getOperator()) {
                case NEGATE -> -operand;
                case ABSOLUTE -> Math.abs(operand);
                case SINE -> Math.sin(operand);
                case COSINE -> Math.cos(operand);
                case LOGARITHM -> Math.log(operand);
            };
        }

        @Override
        public Double visitVariable(VariableExpression expr) {
            Double value = variables.get(expr.getName());
            if (value == null) {
                throw new IllegalArgumentException("Undefined variable: " + expr.getName());
            }
            return value;
        }

        @Override
        public Double visitFunction(FunctionExpression expr) {
            List<Double> args = expr.getArguments().stream()
                .map(arg -> arg.accept(this))
                .toList();

            return switch (expr.getFunctionName()) {
                case "min" -> args.stream().min(Double::compareTo).orElse(0.0);
                case "max" -> args.stream().max(Double::compareTo).orElse(0.0);
                case "sum" -> args.stream().mapToDouble(Double::doubleValue).sum();
                case "avg" -> args.stream().mapToDouble(Double::doubleValue).average().orElse(0.0);
                default -> throw new IllegalArgumentException("Unknown function: " + expr.getFunctionName());
            };
        }
    }

    // String representation visitor
    public static class StringVisitor implements Visitor<String> {
        private boolean parenthesize;

        public StringVisitor() {
            this(false);
        }

        public StringVisitor(boolean parenthesize) {
            this.parenthesize = parenthesize;
        }

        @Override
        public String visitNumber(NumberExpression expr) {
            return String.valueOf(expr.getValue());
        }

        @Override
        public String visitBinary(BinaryExpression expr) {
            String left = expr.getLeft().accept(this);
            String right = expr.getRight().accept(this);
            String result = left + " " + expr.getOperator().getSymbol() + " " + right;
            return parenthesize ? "(" + result + ")" : result;
        }

        @Override
        public String visitUnary(UnaryExpression expr) {
            String operand = expr.getOperand().accept(this);
            return expr.getOperator().getSymbol() + "(" + operand + ")";
        }

        @Override
        public String visitVariable(VariableExpression expr) {
            return expr.getName();
        }

        @Override
        public String visitFunction(FunctionExpression expr) {
            String args = expr.getArguments().stream()
                .map(arg -> arg.accept(this))
                .reduce((a, b) -> a + ", " + b)
                .orElse("");
            return expr.getFunctionName() + "(" + args + ")";
        }
    }

    // Derivative visitor (symbolic differentiation)
    public static class DerivativeVisitor implements Visitor<Expression> {
        private final String variable;

        public DerivativeVisitor(String variable) {
            this.variable = Objects.requireNonNull(variable);
        }

        @Override
        public Expression visitNumber(NumberExpression expr) {
            return new NumberExpression(0); // d/dx(c) = 0
        }

        @Override
        public Expression visitBinary(BinaryExpression expr) {
            Expression left = expr.getLeft();
            Expression right = expr.getRight();
            Expression dLeft = left.accept(this);
            Expression dRight = right.accept(this);

            return switch (expr.getOperator()) {
                case ADD, SUBTRACT -> new BinaryExpression(dLeft, expr.getOperator(), dRight);
                case MULTIPLY -> // Product rule: (uv)' = u'v + uv'
                    new BinaryExpression(
                        new BinaryExpression(dLeft, BinaryExpression.BinaryOperator.MULTIPLY, right),
                        BinaryExpression.BinaryOperator.ADD,
                        new BinaryExpression(left, BinaryExpression.BinaryOperator.MULTIPLY, dRight)
                    );
                case DIVIDE -> // Quotient rule: (u/v)' = (u'v - uv')/v²
                    new BinaryExpression(
                        new BinaryExpression(
                            new BinaryExpression(dLeft, BinaryExpression.BinaryOperator.MULTIPLY, right),
                            BinaryExpression.BinaryOperator.SUBTRACT,
                            new BinaryExpression(left, BinaryExpression.BinaryOperator.MULTIPLY, dRight)
                        ),
                        BinaryExpression.BinaryOperator.DIVIDE,
                        new BinaryExpression(right, BinaryExpression.BinaryOperator.POWER, new NumberExpression(2))
                    );
                case POWER -> // Power rule (simplified for constant exponent)
                    new BinaryExpression(
                        new BinaryExpression(right, BinaryExpression.BinaryOperator.MULTIPLY,
                            new BinaryExpression(left, BinaryExpression.BinaryOperator.POWER,
                                new BinaryExpression(right, BinaryExpression.BinaryOperator.SUBTRACT, new NumberExpression(1)))),
                        BinaryExpression.BinaryOperator.MULTIPLY,
                        dLeft
                    );
            };
        }

        @Override
        public Expression visitUnary(UnaryExpression expr) {
            Expression operand = expr.getOperand();
            Expression dOperand = operand.accept(this);

            Expression derivative = switch (expr.getOperator()) {
                case NEGATE -> new UnaryExpression(UnaryExpression.UnaryOperator.NEGATE, dOperand);
                case ABSOLUTE -> // d/dx|u| = u/|u| * u' (simplified)
                    new BinaryExpression(
                        new BinaryExpression(operand, BinaryExpression.BinaryOperator.DIVIDE, expr),
                        BinaryExpression.BinaryOperator.MULTIPLY,
                        dOperand
                    );
                case SINE -> // d/dx(sin(u)) = cos(u) * u'
                    new BinaryExpression(
                        new UnaryExpression(UnaryExpression.UnaryOperator.COSINE, operand),
                        BinaryExpression.BinaryOperator.MULTIPLY,
                        dOperand
                    );
                case COSINE -> // d/dx(cos(u)) = -sin(u) * u'
                    new BinaryExpression(
                        new UnaryExpression(UnaryExpression.UnaryOperator.NEGATE,
                            new UnaryExpression(UnaryExpression.UnaryOperator.SINE, operand)),
                        BinaryExpression.BinaryOperator.MULTIPLY,
                        dOperand
                    );
                case LOGARITHM -> // d/dx(ln(u)) = u'/u
                    new BinaryExpression(dOperand, BinaryExpression.BinaryOperator.DIVIDE, operand);
            };

            return derivative;
        }

        @Override
        public Expression visitVariable(VariableExpression expr) {
            // d/dx(x) = 1 if x is the variable, 0 otherwise
            return expr.getName().equals(variable) ?
                new NumberExpression(1) : new NumberExpression(0);
        }

        @Override
        public Expression visitFunction(FunctionExpression expr) {
            // Simplified function differentiation
            List<Expression> argDerivatives = expr.getArguments().stream()
                .map(arg -> arg.accept(this))
                .toList();

            // This is a simplified implementation
            return new FunctionExpression("d_" + expr.getFunctionName(), argDerivatives);
        }
    }

    // Factory methods for creating expressions
    public static NumberExpression number(double value) {
        return new NumberExpression(value);
    }

    public static VariableExpression variable(String name) {
        return new VariableExpression(name);
    }

    public static BinaryExpression add(Expression left, Expression right) {
        return new BinaryExpression(left, BinaryExpression.BinaryOperator.ADD, right);
    }

    public static BinaryExpression multiply(Expression left, Expression right) {
        return new BinaryExpression(left, BinaryExpression.BinaryOperator.MULTIPLY, right);
    }

    public static UnaryExpression sin(Expression operand) {
        return new UnaryExpression(UnaryExpression.UnaryOperator.SINE, operand);
    }

    public static FunctionExpression function(String name, Expression... args) {
        return new FunctionExpression(name, Arrays.asList(args));
    }
}
""",
    )

    run_updater(java_nested_project, mock_ingestor, skip_if_missing="java")

    project_name = java_nested_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.VisitorPattern.Expression",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
