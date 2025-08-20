"""
Java edge cases parsing and relationship testing.
Tests Java-specific edge cases like empty classes/interfaces, single-line vs multi-line constructs,
Unicode identifiers, long qualified names, deeply nested generics, and parsing edge cases specific to Java syntax.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_edge_cases_project(temp_repo: Path) -> Path:
    """Create a Java project structure for edge case testing."""
    project_path = temp_repo / "java_edge_cases"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_empty_classes_and_interfaces(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of empty classes and interfaces."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "EmptyTypes.java"
    )
    test_file.write_text(
        """
package com.example;

// Completely empty class
public class EmptyClass {
}

// Empty class with just whitespace
public class WhitespaceClass {



}

// Empty interface
public interface EmptyInterface {
}

// Empty abstract class
public abstract class EmptyAbstractClass {
}

// Empty enum
public enum EmptyEnum {
}

// Empty annotation
public @interface EmptyAnnotation {
}

// Minimal classes with single elements
public class SingleFieldClass {
    private int field;
}

public class SingleMethodClass {
    public void method() {}
}

public class SingleConstructorClass {
    public SingleConstructorClass() {}
}

// Interface with only default methods (effectively empty behavior)
public interface DefaultOnlyInterface {
    default void defaultMethod() {}
}

// Class with only static elements
public class StaticOnlyClass {
    public static final String CONSTANT = "value";
    public static void staticMethod() {}
}

// Abstract class with only abstract methods
public abstract class AbstractOnlyClass {
    public abstract void abstractMethod();
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify all empty types were detected
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}
    {call[0][1]["qualified_name"] for call in enum_calls}

    # Verify at least some empty structures were detected
    assert len(created_classes) >= 5, "Should detect multiple empty classes"
    assert len(created_interfaces) >= 1, "Should detect empty interfaces"


def test_single_line_vs_multiline_constructs(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of single-line vs multi-line Java constructs."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "LineFormats.java"
    )
    test_file.write_text(
        """
package com.example;

// Single-line class
public class SingleLineClass { private int x; public int getX() { return x; } }

// Multi-line equivalent
public class MultiLineClass {
    private int x;

    public int getX() {
        return x;
    }
}

// Single-line interface
public interface SingleLineInterface { void method(); default void defaultMethod() { System.out.println("default"); } }

// Multi-line equivalent
public interface MultiLineInterface {
    void method();

    default void defaultMethod() {
        System.out.println("default");
    }
}

// Single-line enum
public enum SingleLineEnum { VALUE1, VALUE2, VALUE3; }

// Multi-line enum
public enum MultiLineEnum {
    VALUE1,
    VALUE2,
    VALUE3;
}

// Single-line method chains
public class MethodChains {
    public String singleLineChain() { return "test".toUpperCase().trim().substring(1); }

    public String multiLineChain() {
        return "test"
            .toUpperCase()
            .trim()
            .substring(1);
    }
}

// Single-line generics vs multi-line
public class GenericFormats<T> {
    public <U, V> Map<U, List<V>> singleLineGeneric() { return new HashMap<>(); }

    public <U, V> Map<U, List<V>> multiLineGeneric() {
        return new HashMap<>();
    }
}

// Single-line lambda vs multi-line
public class LambdaFormats {
    public void singleLineLambda() {
        List<String> list = Arrays.asList("a", "b", "c");
        list.forEach(s -> System.out.println(s));
    }

    public void multiLineLambda() {
        List<String> list = Arrays.asList("a", "b", "c");
        list.forEach(s -> {
            System.out.println("Processing: " + s);
        });
    }
}

// Compressed vs expanded array initializations
public class ArrayFormats {
    private int[] singleLineArray = {1, 2, 3, 4, 5};

    private int[] multiLineArray = {
        1,
        2,
        3,
        4,
        5
    };
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify both single-line and multi-line formats are parsed correctly
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}
    {call[0][1]["qualified_name"] for call in enum_calls}

    # Should detect both single-line and multi-line versions
    assert any("SingleLineClass" in qn for qn in created_classes)
    assert any("MultiLineClass" in qn for qn in created_classes)
    assert any("SingleLineInterface" in qn for qn in created_interfaces)
    assert any("MultiLineInterface" in qn for qn in created_interfaces)


def test_unicode_identifiers(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of Unicode identifiers in Java."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "UnicodeIdentifiers.java"
    )
    test_file.write_text(
        """
package com.example;

// Classes with Unicode names
public class Классический {
    private String название;

    public String getНазвание() {
        return название;
    }
}

public class 中文类 {
    private int 数字;

    public void 设置数字(int 值) {
        this.数字 = 值;
    }
}

// Interface with Unicode name
public interface インターフェース {
    void メソッド();
}

// Enum with Unicode values
public enum Цвета {
    КРАСНЫЙ, ЗЕЛЕНЫЙ, СИНИЙ;
}

// Arabic identifiers
public class العربية {
    private String النص;

    public String الحصولعلىالنص() {
        return النص;
    }
}

// Mixed Unicode and ASCII
public class MixedUnicode混合 {
    private String englishField;
    private String 中文字段;
    private String русскоеПоле;

    public void processТекст(String input) {
        System.out.println("Processing: " + input);
    }
}

// Unicode in method parameters and return types
public class UnicodeParameters {
    public Map<String, Список> createМап() {
        return new HashMap<>();
    }

    public void process参数(String 名字, int 年龄, boolean активный) {
        // Implementation
    }
}

// Generic class with Unicode type parameters
public class Container<Т> {
    private Т значение;

    public Т getЗначение() {
        return значение;
    }

    public void setЗначение(Т значение) {
        this.значение = значение;
    }
}

// Custom type using Unicode
class Список extends java.util.ArrayList<String> {
    public void добавить(String элемент) {
        add(элемент);
    }
}

// Constants with Unicode names
public class UnicodeConstants {
    public static final String МАКСИМУМ = "max";
    public static final int 最大值 = 100;
    public static final double π = 3.14159;
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify Unicode identifiers are handled (may depend on parser capabilities)
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # At minimum, should detect some Unicode-named classes
    assert len(created_classes) >= 3, "Should detect classes with Unicode names"


def test_long_qualified_names(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of very long qualified names and deep package structures."""
    # Create deep package structure
    deep_path = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "very"
        / "deep"
        / "package"
        / "structure"
        / "with"
        / "many"
        / "levels"
    )
    deep_path.mkdir(parents=True)

    test_file = deep_path / "VeryLongQualifiedNames.java"
    test_file.write_text(
        """
package com.example.very.deep.package.structure.with.many.levels;

import java.util.concurrent.ConcurrentHashMap;
import java.util.function.BiFunction;

public class VeryLongQualifiedNamesWithExtremelyDescriptiveClassNameThatExceedsNormalLengthLimitations {

    private Map<String, List<Set<Optional<ComplexNestedGenericTypeWithVeryLongNameThatTestsParsingLimits>>>>
        extremelyLongFieldNameThatTestsFieldNameLengthLimitationsInJavaCode;

    public VeryLongMethodNameThatTestsMethodNameLengthLimitationsAndParsingCapabilitiesOfTheSystem
        createVeryLongMethodNameThatReturnsComplexType() {
        return new VeryLongMethodNameThatTestsMethodNameLengthLimitationsAndParsingCapabilitiesOfTheSystem();
    }

    public <VeryLongTypeParameterNameThatTestsGenericTypeParameterLengthLimitations,
            AnotherVeryLongTypeParameterNameForTestingPurposes,
            YetAnotherExtremelyLongTypeParameterNameToTestParsingLimits>
    Map<VeryLongTypeParameterNameThatTestsGenericTypeParameterLengthLimitations,
        List<AnotherVeryLongTypeParameterNameForTestingPurposes>>
    processVeryLongGenericMethodWithExtremelyLongParameterNames(
        VeryLongTypeParameterNameThatTestsGenericTypeParameterLengthLimitations firstParameterWithVeryLongName,
        AnotherVeryLongTypeParameterNameForTestingPurposes secondParameterWithEvenLongerName,
        YetAnotherExtremelyLongTypeParameterNameToTestParsingLimits thirdParameterWithExtremelyLongName
    ) {
        return new HashMap<>();
    }

    // Nested class with long name
    public static class NestedClassWithVeryLongNameThatTestsNestedClassNameLengthLimitations {

        // Inner class with even longer name
        public class InnerClassWithExtremelyLongNameThatTestsInnerClassNameLengthLimitationsAndParsingCapabilities {

            private String fieldWithVeryLongNameThatTestsFieldNameLengthLimitationsInNestedClasses;

            public void methodWithVeryLongNameThatTestsMethodNameLengthLimitationsInInnerClasses() {
                // Implementation
            }
        }
    }
}

// Another class with different long pattern
class ComplexNestedGenericTypeWithVeryLongNameThatTestsParsingLimits {
    // Implementation
}

class VeryLongMethodNameThatTestsMethodNameLengthLimitationsAndParsingCapabilitiesOfTheSystem {
    // Implementation
}

// Interface with long name
interface InterfaceWithVeryLongNameThatTestsInterfaceNameLengthLimitationsAndParsingCapabilities {
    void methodWithVeryLongNameInInterface();
}

// Enum with long name
enum EnumWithVeryLongNameThatTestsEnumNameLengthLimitationsAndParsingCapabilities {
    VALUE_WITH_VERY_LONG_NAME_THAT_TESTS_ENUM_VALUE_LENGTH_LIMITATIONS,
    ANOTHER_VALUE_WITH_EVEN_LONGER_NAME_FOR_TESTING_PURPOSES,
    YET_ANOTHER_VALUE_WITH_EXTREMELY_LONG_NAME_TO_TEST_PARSING_LIMITS;
}

// Annotation with long name
@interface AnnotationWithVeryLongNameThatTestsAnnotationNameLengthLimitationsAndParsingCapabilities {
    String valueWithVeryLongNameThatTestsAnnotationValueNameLengthLimitations() default "";
    int priorityWithLongNameForTestingPurposes() default 0;
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify long qualified names are parsed correctly
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Should handle long qualified names
    long_class_found = any(
        "VeryLongQualifiedNamesWithExtremelyDescriptive" in qn for qn in created_classes
    )
    assert long_class_found, "Should detect classes with very long names"


def test_deeply_nested_generics(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of deeply nested generic types."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DeeplyNestedGenerics.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;

public class DeeplyNestedGenerics {

    // Level 1: Simple generic
    private List<String> simpleList;

    // Level 2: Nested generic
    private Map<String, List<Integer>> mapOfLists;

    // Level 3: Triple nested
    private Map<String, List<Set<String>>> mapOfListsOfSets;

    // Level 4: Quadruple nested
    private Map<String, List<Set<Map<Integer, String>>>> mapOfListsOfSetsOfMaps;

    // Level 5: Quintuple nested
    private Map<String, List<Set<Map<Integer, List<String>>>>> mapOfListsOfSetsOfMapsOfLists;

    // Level 6: Sextuple nested with wildcards
    private Map<String, List<Set<Map<Integer, List<? extends Collection<? super String>>>>>>
        extremelyNestedWithWildcards;

    // Level 7: Maximum nesting with complex bounds
    private Map<String,
                List<Set<
                    Map<Integer,
                        List<? extends Collection<
                            ? super Map<String,
                                       List<? extends Comparable<? super String>>>>>>>>>
        maximumNestingLevel;

    // Nested generics with type bounds
    public <T extends Comparable<T>,
            U extends Collection<? extends T>,
            V extends Map<? super T, ? extends U>>
    Optional<Map<String, List<Set<V>>>> processComplexBounds(V input) {
        return Optional.empty();
    }

    // Recursive generic bounds with deep nesting
    public static class RecursiveContainer<T extends RecursiveContainer<T>> {
        private Map<String, List<Optional<T>>> children;

        public <U extends RecursiveContainer<U> & Comparable<U>>
        Map<String, List<Set<Optional<U>>>> createNestedStructure() {
            return new HashMap<>();
        }
    }

    // Function types with deep nesting
    private Function<
        Map<String, List<Integer>>,
        BiFunction<
            Set<String>,
            Predicate<Map<Integer, String>>,
            Optional<List<Set<Map<String, Integer>>>>
        >
    > complexFunctionType;

    // Array types with nested generics
    private List<String>[] arrayOfGenericLists;
    private Map<String, Integer>[][] twoDimensionalArrayOfMaps;
    private Set<List<Map<String, Integer>>>[] arrayOfNestedGenerics;

    // Method with extreme generic complexity
    public <A, B extends Collection<A>, C extends Map<A, B>>
    CompletableFuture<Optional<Map<String, List<Set<C>>>>>
    processExtremeGenerics(
        Supplier<? extends Map<A, ? extends B>> supplier,
        Function<? super B, ? extends C> mapper,
        BiPredicate<? super A, ? super C> filter
    ) {
        return CompletableFuture.completedFuture(Optional.empty());
    }

    // Nested class with its own complex generics
    public static class NestedGenericClass<
        T extends Map<String, ? extends List<? super Integer>>,
        U extends Collection<? extends T>
    > {
        private Map<String, List<Set<Optional<T>>>> nestedField;

        public <V extends Comparable<V> & Serializable>
        Stream<Map<V, List<Set<Optional<U>>>>> complexNestedMethod() {
            return Stream.empty();
        }
    }

    // Generic inheritance chain
    public static class GenericBase<T> {
        protected T value;
    }

    public static class GenericMiddle<T, U extends Collection<T>> extends GenericBase<Map<String, U>> {
        // Inherits complex generic type
    }

    public static class GenericDerived extends GenericMiddle<String, List<String>> {
        // Final concrete implementation of deeply nested generic chain
    }

    // Wildcard variance with deep nesting
    public void varianceExample(
        List<? extends Map<String, ? extends Collection<? extends Number>>> input,
        List<? super Map<String, ? super Collection<? super Integer>>> output
    ) {
        // Complex variance relationships
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify deeply nested generics are parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Should detect main class and nested classes
    assert any("DeeplyNestedGenerics" in qn for qn in created_classes)


def test_parsing_edge_cases_syntax(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of Java syntax edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SyntaxEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example;

import static java.lang.Math.*;
import static java.util.Collections.EMPTY_LIST;

public class SyntaxEdgeCases {

    // Unicode escape sequences in identifiers
    private String unicode\\u0046ield = "field";

    // Method with unicode escapes
    public void method\\u0057ithUnicode() {
        // Implementation
    }

    // Numeric literals edge cases
    private int binary = 0b1010_1111_0000_1111;
    private int hex = 0xFF_FF_FF_FF;
    private long longWithUnderscore = 1_000_000_000L;
    private double scientific = 1.23e-4;
    private float floatLiteral = 3.14f;

    // String literal edge cases
    private String emptyString = "";
    private String stringWithEscapes = "line1\\nline2\\ttab\\rreturn\\\\backslash\\"quote";
    private String unicodeString = "\\u0048\\u0065\\u006c\\u006c\\u006f"; // "Hello"

    // Character literals
    private char singleQuote = '\\'';
    private char backslash = '\\\\';
    private char tab = '\\t';
    private char unicodeChar = '\\u0041'; // 'A'

    // Complex array declarations
    private int[] simpleArray;
    private int simpleArray2[];
    private int[][] twoDArray;
    private int[] twoDArray2[];
    private int[][][] threeDArray;

    // Generic array edge cases
    private List<String>[] genericArray;
    private Map<String, Integer>[] mapArray;

    // Varargs edge cases
    public void varargs(String... args) {}
    public void multipleParams(int x, String... args) {}
    public <T> void genericVarargs(T... args) {}

    // Method reference edge cases
    private Supplier<String> methodRef1 = String::new;
    private Function<String, Integer> methodRef2 = String::length;
    private BiFunction<String, String, String> methodRef3 = String::concat;
    private Consumer<String> methodRef4 = System.out::println;

    // Lambda edge cases
    private Runnable emptyLambda = () -> {};
    private Consumer<String> singleParamLambda = s -> System.out.println(s);
    private Consumer<String> singleParamWithParens = (s) -> System.out.println(s);
    private BiFunction<Integer, Integer, Integer> multiParam = (a, b) -> a + b;

    // Complex lambda with type annotations
    private BiFunction<String, String, String> typedLambda =
        (String s1, String s2) -> s1 + s2;

    // Lambda with block body
    private Function<String, String> blockLambda = s -> {
        String result = s.toUpperCase();
        return result.trim();
    };

    // Anonymous class edge cases
    private Runnable anonymousRunnable = new Runnable() {
        @Override
        public void run() {
            System.out.println("Running");
        }
    };

    // Generic anonymous class
    private Comparator<String> anonymousComparator = new Comparator<String>() {
        @Override
        public int compare(String s1, String s2) {
            return s1.length() - s2.length();
        }
    };

    // Nested anonymous classes
    private Supplier<Runnable> nestedAnonymous = new Supplier<Runnable>() {
        @Override
        public Runnable get() {
            return new Runnable() {
                @Override
                public void run() {
                    System.out.println("Nested anonymous");
                }
            };
        }
    };

    // Complex initialization blocks
    {
        // Instance initialization block
        System.out.println("Instance init");
    }

    static {
        // Static initialization block
        System.out.println("Static init");
    }

    // Constructor with complex parameter list
    public SyntaxEdgeCases(
        String param1,
        int param2,
        List<? extends String> param3,
        Map<String, ? super Integer> param4,
        Function<String, Integer>... param5
    ) {
        // Constructor body
    }

    // Method with throws clause
    public void methodWithExceptions()
        throws IllegalArgumentException,
               IllegalStateException,
               RuntimeException {
        // Implementation
    }

    // Generic method with complex bounds
    public <T extends Comparable<T> & Serializable & Cloneable>
    Optional<T> complexGenericMethod(T input)
        throws IllegalArgumentException {
        return Optional.ofNullable(input);
    }

    // Annotation edge cases
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Deprecated
    public void annotatedMethod() {
        // Implementation
    }

    // Annotation with array values
    @MyAnnotation(values = {"a", "b", "c"}, numbers = {1, 2, 3})
    public void arrayAnnotation() {
        // Implementation
    }
}

// Annotation definition
@interface MyAnnotation {
    String[] values() default {};
    int[] numbers() default {};
}

// Class with complex inheritance
class ComplexInheritance
    extends java.util.AbstractList<String>
    implements java.util.List<String>,
               java.lang.Cloneable,
               java.io.Serializable {

    @Override
    public String get(int index) {
        return null;
    }

    @Override
    public int size() {
        return 0;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify syntax edge cases are parsed correctly
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    assert any("SyntaxEdgeCases" in qn for qn in created_classes)


def test_malformed_but_valid_syntax(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of syntactically valid but unusual Java code."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MalformedButValid.java"
    )
    test_file.write_text(
        """
package
    com
    .
    example
    ;

import
    java
    .
    util
    .
    *
    ;

public
class
MalformedButValid
{
    private
    int
    field
    ;

    public
    MalformedButValid
    (
    )
    {
    }

    public
    void
    method
    (
    )
    {
        int
        x
        =
        5
        ;

        if
        (
        x
        >
        0
        )
        {
            System
            .
            out
            .
            println
            (
            "positive"
            )
            ;
        }
    }

    public
    <
    T
    >
    void
    genericMethod
    (
    T
    parameter
    )
    {
        // Implementation
    }
}

// Class with excessive spacing
public   class   ExcessiveSpacing   {

    private   String   field   ;

    public   void   method   (   String   param   )   {
        // Implementation
    }
}

// Class with minimal spacing
public class MinimalSpacing{private String field;public void method(String param){}}

// Unusual but valid generic syntax
class WeirdGenerics<T,U,V> {
    private Map<String,List<T>> field1;
    private Set<Map<U,V>> field2;

    public<X,Y>Map<X,Y>method(){return null;}
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify malformed but valid syntax is parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Should handle unusual formatting
    assert len(created_classes) >= 2, "Should detect classes despite unusual formatting"


def test_boundary_value_literals(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of boundary value literals and extreme values."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BoundaryValues.java"
    )
    test_file.write_text(
        """
package com.example;

public class BoundaryValues {

    // Integer boundary values
    private int minInt = Integer.MIN_VALUE;
    private int maxInt = Integer.MAX_VALUE;
    private int minIntLiteral = -2147483648;
    private int maxIntLiteral = 2147483647;

    // Long boundary values
    private long minLong = Long.MIN_VALUE;
    private long maxLong = Long.MAX_VALUE;
    private long minLongLiteral = -9223372036854775808L;
    private long maxLongLiteral = 9223372036854775807L;

    // Float boundary values
    private float minFloat = Float.MIN_VALUE;
    private float maxFloat = Float.MAX_VALUE;
    private float negativeInfinity = Float.NEGATIVE_INFINITY;
    private float positiveInfinity = Float.POSITIVE_INFINITY;
    private float notANumber = Float.NaN;
    private float minNormal = Float.MIN_NORMAL;

    // Double boundary values
    private double minDouble = Double.MIN_VALUE;
    private double maxDouble = Double.MAX_VALUE;
    private double doubleNegInf = Double.NEGATIVE_INFINITY;
    private double doublePosInf = Double.POSITIVE_INFINITY;
    private double doubleNaN = Double.NaN;

    // Byte boundary values
    private byte minByte = Byte.MIN_VALUE;
    private byte maxByte = Byte.MAX_VALUE;
    private byte minByteLiteral = -128;
    private byte maxByteLiteral = 127;

    // Short boundary values
    private short minShort = Short.MIN_VALUE;
    private short maxShort = Short.MAX_VALUE;
    private short minShortLiteral = -32768;
    private short maxShortLiteral = 32767;

    // Character boundary values
    private char minChar = Character.MIN_VALUE;
    private char maxChar = Character.MAX_VALUE;
    private char nullChar = '\\u0000';
    private char maxUnicode = '\\uFFFF';

    // Extreme numeric literals
    private double verySmall = 1e-323;
    private double veryLarge = 1e308;
    private float verySmallFloat = 1e-45f;
    private float veryLargeFloat = 1e38f;

    // Binary literals with extreme values
    private int allOnes = 0b11111111111111111111111111111111;
    private int alternating = 0b10101010101010101010101010101010;
    private long longBinary = 0b1111111111111111111111111111111111111111111111111111111111111111L;

    // Hexadecimal extreme values
    private int hexMax = 0xFFFFFFFF;
    private long hexLongMax = 0xFFFFFFFFFFFFFFFFL;
    private int hexPattern = 0xDEADBEEF;
    private int hexZero = 0x0;

    // Octal values (deprecated but valid)
    private int octalValue = 0777;
    private int octalZero = 00;

    // String boundary cases
    private String emptyString = "";
    private String singleChar = "a";
    private String nullEscape = "\\u0000";
    private String allEscapes = "\\n\\r\\t\\b\\f\\'\\\"\\\\";

    // Array boundary cases
    private int[] emptyArray = {};
    private int[] singleElement = {42};
    private String[] arrayWithNulls = {null, null, null};

    // Method testing extreme parameter counts
    public void noParams() {}

    public void oneParam(int a) {}

    public void manyParams(
        int a, int b, int c, int d, int e, int f, int g, int h, int i, int j,
        int k, int l, int m, int n, int o, int p, int q, int r, int s, int t,
        int u, int v, int w, int x, int y, int z
    ) {}

    // Method testing extreme generic bounds
    public <A, B, C, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Y, Z>
    void extremeGenerics() {}

    // Testing boundary string lengths
    public String shortString() {
        return "x";
    }

    public String longString() {
        return "This is a very long string that tests the parsing of lengthy string literals " +
               "to ensure that the parser can handle strings of various lengths without issues " +
               "and continues to parse the rest of the file correctly even when encountering " +
               "extremely long string values that might cause buffer overflows or parsing errors " +
               "in poorly implemented parsers that do not handle edge cases properly";
    }

    // Boundary cases for numeric operations
    public void numericBoundaries() {
        int result1 = Integer.MAX_VALUE + 1; // Overflow
        int result2 = Integer.MIN_VALUE - 1; // Underflow
        double result3 = Double.MAX_VALUE * 2; // Infinity
        double result4 = 1.0 / 0.0; // Positive infinity
        double result5 = -1.0 / 0.0; // Negative infinity
        double result6 = 0.0 / 0.0; // NaN
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify boundary values are parsed correctly
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    assert any("BoundaryValues" in qn for qn in created_classes)


def test_comment_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing with various comment edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CommentEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example; // Package comment

import java.util.*; /* Import comment */

/**
 * Javadoc with special characters: @param <>&"'
 * Multiple lines with * alignment
 *   - Bullet points
 *   - More bullets
 * @author Someone
 * @since 1.0
 * @see SomeClass
 */
public class CommentEdgeCases {

    // Single line comment
    private String field1;

    /* Multi-line comment */ private String field2;

    private String field3; // End of line comment

    /*
     * Multi-line comment
     * with multiple lines
     * and various content
     */
    private String field4;

    /** Javadoc on one line */ private String field5;

    /**
     * Javadoc with code examples:
     * <pre>
     * {@code
     * String example = "hello";
     * System.out.println(example);
     * }
     * </pre>
     */
    public void methodWithComplexJavadoc() {
        // Method implementation
    }

    // Comment with Unicode: 中文注释 русский комментарий
    public void unicodeCommentMethod() {}

    /* Comment with /* nested comment markers */
    public void nestedCommentMarkers() {}

    // Comment with URLs: https://example.com and file://path
    public void urlInComment() {}

    /*
     * Comment with special symbols:
     * !@#$%^&*()_+{}|:"<>?`~-=[]\\;',./
     */
    public void specialSymbolsInComment() {}

    public void methodWithInlineComments() {
        int x = 5; // Inline comment
        /* Another inline */ int y = 10;
        int z = x /* comment in expression */ + y;

        if (x > 0) { // Condition comment
            System.out.println("positive"); // Print comment
        } /* End if comment */
    }

    /**
     * Javadoc with HTML:
     * <p>This is a paragraph.</p>
     * <ul>
     * <li>Item 1</li>
     * <li>Item 2</li>
     * </ul>
     * <b>Bold text</b> and <i>italic text</i>
     */
    public void htmlInJavadoc() {}

    /*
     * TODO: This is a TODO comment
     * FIXME: This needs to be fixed
     * NOTE: Important note here
     * HACK: Temporary workaround
     * XXX: Warning marker
     */
    public void annotatedComments() {}

    // Comment with escaped characters: \\n \\t \\r \\" \\'
    public void escapedCharsInComment() {}

    /*
     * Very long comment that tests the parsing of lengthy comments to ensure
     * that the parser can handle comments of various lengths without issues
     * and continues to parse the rest of the file correctly even when
     * encountering extremely long comments that might cause buffer issues
     * in parsers that do not handle large text blocks properly or efficiently
     * making sure edge cases are covered thoroughly and comprehensively
     */
    public void veryLongComment() {}

    // Comment at end of file without newline
} // Class end comment
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify comments don't break parsing
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    assert any("CommentEdgeCases" in qn for qn in created_classes)


def test_whitespace_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing with various whitespace edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "WhitespaceEdgeCases.java"
    )
    # Note: Using actual whitespace characters in the string
    test_file.write_text(
        "package com.example;\n\n"
        + "import java.util.*;\n\n"
        + "public class WhitespaceEdgeCases {\n\n"
        + "    // Tabs vs spaces mixing\n"
        + "\tprivate String\ttabField;\n"
        + "    private String    spaceField;\n"
        + "\t    private String mixed;\n\n"
        + "    // Various line endings\n"
        + "    public void method1() {}\r\n"
        + "    public void method2() {}\n"
        + "    public void method3() {}\r\n\n"
        + "    // Multiple consecutive spaces\n"
        + "    public     void     spacedMethod     (     String     param     )     {\n"
        + "        //     Implementation\n"
        + "    }\n\n"
        + "    // Form feed and other whitespace\n"
        + "    public\\fvoid\\fformFeedMethod() {\n"
        + '        \\fSystem.out.println(\\"test\\");\\f\n'
        + "    }\n\n"
        + "    // Vertical tab (if supported)\n"
        + "    public\\u000Bvoid\\u000BverticalTabMethod() {}\n\n"
        + "    // No space around operators\n"
        + "    public void operatorSpacing() {\n"
        + "        int x=5+3*2/4-1%2;\n"
        + "        boolean b=x>0&&x<10||x==100;\n"
        + '        String s=\\"hello\\"+\\"world\\";\n'
        + "    }\n\n"
        + "    // Excessive whitespace\n"
        + "    public\\n\\n\\nvoid\\n\\n\\nexcessiveNewlines() {\n\n\n\n"
        + '        \\n\\n\\nSystem.out.println(\\"test\\");\\n\\n\\n\n'
        + "    }\n\n"
        + "    // Mixed indentation\n"
        + "\\tpublic void mixedIndentation() {\n"
        + "\\t    if (true) {\n"
        + '\\t\\tSystem.out.println(\\"tab indent\\");\n'
        + '        \\tSystem.out.println(\\"space then tab\\");\n'
        + '\\t        System.out.println(\\"tab then space\\");\n'
        + "    \\t}\n"
        + "\\t}\n\n"
        + "    // Zero-width characters (if any)\n"
        + "    public void zeroWidthMethod\\u200B() {\n"
        + "        // Zero-width space in method name\n"
        + "    }\n\n"
        + "    // Unicode whitespace\n"
        + "    public\\u0020void\\u00A0unicodeSpaces() {\n"
        + "        // Regular space: \\u0020, Non-breaking space: \\u00A0\n"
        + "    }\n"
        + "}\n"
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify whitespace variations don't break parsing
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    assert any("WhitespaceEdgeCases" in qn for qn in created_classes)


def test_package_and_import_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of package and import edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "PackageImportEdgeCases.java"
    )
    test_file.write_text(
        """
// File-level comment before package
package com.example;

// Multiple import styles
import java.util.List;
import java.util.ArrayList;
import java.util.Map;
import java.util.HashMap;
import java.util.*;

// Static imports
import static java.lang.Math.PI;
import static java.lang.Math.sin;
import static java.lang.Math.cos;
import static java.lang.Math.*;
import static java.util.Collections.*;

// Import on demand with static
import static java.util.Arrays.*;

// Fully qualified names without imports
public class PackageImportEdgeCases {

    // Using imported types
    private List<String> importedList = new ArrayList<>();
    private Map<String, Integer> importedMap = new HashMap<>();

    // Using static imports
    private double piValue = PI;
    private double sinValue = sin(PI / 2);

    // Using fully qualified names
    private java.util.concurrent.ConcurrentHashMap<String, String> fullyQualified;
    private java.util.concurrent.atomic.AtomicInteger atomicCounter;
    private java.time.LocalDateTime dateTime;
    private java.nio.file.Path filePath;

    // Mix of imported and fully qualified
    public java.util.Optional<List<String>> mixedMethod(
        java.util.Set<String> fullyQualifiedParam,
        List<String> importedParam
    ) {
        return java.util.Optional.empty();
    }

    // Using wildcard imports
    public void wildcardImportUsage() {
        // From java.util.*
        Set<String> set = new HashSet<>();
        Queue<String> queue = new LinkedList<>();

        // From static Math.*
        double result = sqrt(pow(3, 2) + pow(4, 2));

        // From static Collections.*
        List<String> unmodifiable = unmodifiableList(new ArrayList<>());
    }

    // Nested class with same name as imported class
    public static class List {
        // This shadows java.util.List
        private String value;

        public String getValue() {
            return value;
        }
    }

    // Method using both shadowed and imported List
    public void shadowingExample() {
        // This uses our nested List class
        List localList = new List();

        // This uses java.util.List (fully qualified to avoid confusion)
        java.util.List<String> utilList = new ArrayList<>();
    }

    // Import conflicts resolution
    public void importConflicts() {
        // When there are conflicts, must use fully qualified names
        java.awt.List awtList = new java.awt.List();
        java.util.List<String> utilList = new ArrayList<>();

        java.util.Date utilDate = new java.util.Date();
        java.sql.Date sqlDate = new java.sql.Date(System.currentTimeMillis());
    }

    // Using package-private classes (conceptually)
    PackagePrivateClass packagePrivate = new PackagePrivateClass();

    // Inner classes and imports
    public class InnerClass {
        // Inner class can use same imports
        private List<String> innerList = new ArrayList<>();
        private Map<String, Integer> innerMap = new HashMap<>();
    }

    // Static nested class and imports
    public static class StaticNestedClass {
        // Static nested class can use same imports
        private static final double NESTED_PI = PI;

        public static List<String> createList() {
            return new ArrayList<>();
        }
    }
}

// Package-private class (no explicit modifier)
class PackagePrivateClass {
    String packageField;

    void packageMethod() {
        // Can use same imports as public class
        List<String> list = new ArrayList<>();
    }
}

// Another public class in same file (unusual but valid)
class AnotherClassInSameFile {
    private Map<String, String> map = new HashMap<>();

    // Uses same imports as main class
    public void method() {
        double value = cos(PI);
        List<String> emptyList = emptyList();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify package and import edge cases are handled
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Should detect multiple classes in same file
    assert len(created_classes) >= 2, "Should detect multiple classes in same file"
    assert any("PackageImportEdgeCases" in qn for qn in created_classes)


def test_modifier_combinations_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex modifier combinations."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ModifierCombinations.java"
    )
    test_file.write_text(
        """
package com.example;

import java.io.Serializable;

public final class ModifierCombinations {

    // Field modifier combinations
    public static final String PUBLIC_STATIC_FINAL = "constant";
    private static final String PRIVATE_STATIC_FINAL = "private_constant";
    protected static final String PROTECTED_STATIC_FINAL = "protected_constant";
    static final String PACKAGE_STATIC_FINAL = "package_constant";

    public final String PUBLIC_FINAL = "public_final";
    private final String PRIVATE_FINAL = "private_final";
    protected final String PROTECTED_FINAL = "protected_final";
    final String PACKAGE_FINAL = "package_final";

    public static String PUBLIC_STATIC = "public_static";
    private static String PRIVATE_STATIC = "private_static";
    protected static String PROTECTED_STATIC = "protected_static";
    static String PACKAGE_STATIC = "package_static";

    public volatile String PUBLIC_VOLATILE = "volatile";
    private volatile String PRIVATE_VOLATILE = "volatile";
    protected volatile String PROTECTED_VOLATILE = "volatile";
    volatile String PACKAGE_VOLATILE = "volatile";

    public transient String PUBLIC_TRANSIENT = "transient";
    private transient String PRIVATE_TRANSIENT = "transient";
    protected transient String PROTECTED_TRANSIENT = "transient";
    transient String PACKAGE_TRANSIENT = "transient";

    // Method modifier combinations
    public static final void publicStaticFinal() {}
    private static final void privateStaticFinal() {}
    protected static final void protectedStaticFinal() {}
    static final void packageStaticFinal() {}

    public final void publicFinal() {}
    private final void privateFinal() {}
    protected final void protectedFinal() {}
    final void packageFinal() {}

    public static void publicStatic() {}
    private static void privateStatic() {}
    protected static void protectedStatic() {}
    static void packageStatic() {}

    public synchronized void publicSynchronized() {}
    private synchronized void privateSynchronized() {}
    protected synchronized void protectedSynchronized() {}
    synchronized void packageSynchronized() {}

    public static synchronized void publicStaticSynchronized() {}
    private static synchronized void privateStaticSynchronized() {}

    public final synchronized void publicFinalSynchronized() {}
    private final synchronized void privateFinalSynchronized() {}

    // Constructor modifier combinations
    public ModifierCombinations() {}
    private ModifierCombinations(int x) {}
    protected ModifierCombinations(String s) {}
    ModifierCombinations(double d) {}

    // Native method (abstract concept, no implementation)
    public native void nativeMethod();
    private native void privateNativeMethod();
    protected native void protectedNativeMethod();
    native void packageNativeMethod();

    public static native void staticNativeMethod();
    private static native void privateStaticNativeMethod();

    // Strictfp methods
    public strictfp double strictfpMethod() { return 1.0; }
    private strictfp double privateStrictfpMethod() { return 1.0; }
    protected strictfp double protectedStrictfpMethod() { return 1.0; }
    strictfp double packageStrictfpMethod() { return 1.0; }

    public static strictfp double staticStrictfpMethod() { return 1.0; }
    public final strictfp double finalStrictfpMethod() { return 1.0; }
    public synchronized strictfp double synchronizedStrictfpMethod() { return 1.0; }
}

// Abstract class with modifier combinations
public abstract class AbstractModifierCombinations {

    // Abstract method combinations
    public abstract void publicAbstract();
    protected abstract void protectedAbstract();
    abstract void packageAbstract();

    public abstract static void publicAbstractStatic() {} // Invalid, but test parser

    // Final methods in abstract class
    public final void publicFinalInAbstract() {}
    protected final void protectedFinalInAbstract() {}

    // Static methods in abstract class
    public static void publicStaticInAbstract() {}
    private static void privateStaticInAbstract() {}
}

// Interface with modifier combinations
public interface InterfaceModifierCombinations {

    // Interface fields (implicitly public static final)
    String INTERFACE_FIELD = "value";
    public String PUBLIC_INTERFACE_FIELD = "public_value";
    static String STATIC_INTERFACE_FIELD = "static_value";
    final String FINAL_INTERFACE_FIELD = "final_value";
    public static final String EXPLICIT_INTERFACE_FIELD = "explicit_value";

    // Interface methods
    void interfaceMethod();
    public void publicInterfaceMethod();

    // Default methods with modifiers
    default void defaultMethod() {}
    public default void publicDefaultMethod() {}

    // Static methods in interface
    static void staticInterfaceMethod() {}
    public static void publicStaticInterfaceMethod() {}

    // Private methods in interface (Java 9+)
    private void privateInterfaceMethod() {}
    private static void privateStaticInterfaceMethod() {}
}

// Enum with modifier combinations
public enum EnumModifierCombinations {
    VALUE1, VALUE2, VALUE3;

    // Enum fields
    private static final String ENUM_CONSTANT = "constant";
    public static final String PUBLIC_ENUM_CONSTANT = "public_constant";

    // Enum methods
    public void publicEnumMethod() {}
    private void privateEnumMethod() {}
    protected void protectedEnumMethod() {}
    void packageEnumMethod() {}

    public static void staticEnumMethod() {}
    private static void privateStaticEnumMethod() {}

    public final void finalEnumMethod() {}
    private final void privateFinalEnumMethod() {}
}

// Nested class modifier combinations
public class NestedClassModifiers {

    // Public nested class
    public static class PublicNestedClass {
        public void method() {}
    }

    // Private nested class
    private static class PrivateNestedClass {
        public void method() {}
    }

    // Protected nested class
    protected static class ProtectedNestedClass {
        public void method() {}
    }

    // Package nested class
    static class PackageNestedClass {
        public void method() {}
    }

    // Final nested class
    public static final class FinalNestedClass {
        public void method() {}
    }

    // Abstract nested class
    public static abstract class AbstractNestedClass {
        public abstract void abstractMethod();
        public void concreteMethod() {}
    }

    // Non-static inner classes
    public class PublicInnerClass {
        public void method() {}
    }

    private class PrivateInnerClass {
        public void method() {}
    }

    protected class ProtectedInnerClass {
        public void method() {}
    }

    class PackageInnerClass {
        public void method() {}
    }

    public final class FinalInnerClass {
        public void method() {}
    }

    public abstract class AbstractInnerClass {
        public abstract void abstractMethod();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify complex modifier combinations are parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}

    # Should detect various types with different modifiers
    assert len(created_classes) >= 3, "Should detect multiple classes with modifiers"
    assert len(created_interfaces) >= 1, "Should detect interface with modifiers"
    assert len(created_enums) >= 1, "Should detect enum with modifiers"


def test_generic_variance_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex generic variance scenarios."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "GenericVarianceEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;

public class GenericVarianceEdgeCases {

    // Simple variance
    private List<? extends String> extendsString;
    private List<? super String> superString;
    private List<?> unboundedWildcard;

    // Nested variance
    private List<? extends List<? extends String>> nestedExtends;
    private List<? super List<? super String>> nestedSuper;
    private Map<? extends String, ? super Integer> mapVariance;

    // Complex variance chains
    private Map<String, List<? extends Set<? super Integer>>> complexVariance1;
    private Set<? extends Map<? super String, ? extends List<? super Number>>> complexVariance2;

    // Variance with generic bounds
    public <T extends Comparable<T>> List<? extends T> boundedVariance(
        List<? super T> input
    ) {
        return new ArrayList<>();
    }

    // Multiple bounded variance
    public <T extends Number & Comparable<T>, U extends Collection<? extends T>>
    Map<? extends String, ? super U> multipleBoundedVariance(
        Set<? extends T> input1,
        List<? super U> input2
    ) {
        return new HashMap<>();
    }

    // Variance in method parameters and return types
    public void varianceParameters(
        Supplier<? extends String> supplier,
        Consumer<? super String> consumer,
        Function<? super String, ? extends Integer> function,
        Predicate<? super String> predicate
    ) {
        // Implementation
    }

    // Recursive variance
    public <T> void recursiveVariance(
        List<? extends List<? extends T>> input
    ) {
        // Implementation
    }

    // Variance with array types
    private List<? extends String[]> arrayVariance1;
    private Set<? super Integer[]> arrayVariance2;
    private Map<String, ? extends List<String>[]> arrayVariance3;

    // Variance in generic class inheritance
    public static class VariantContainer<T> {
        private T value;

        public T getValue() {
            return value;
        }

        public void setValue(T value) {
            this.value = value;
        }
    }

    public static class ExtendingContainer extends VariantContainer<String> {
        // Inherits specific type
    }

    public static class VariantExtending<T> extends VariantContainer<List<? extends T>> {
        // Inherits with variance
    }

    // Variance in interface implementation
    interface VariantInterface<T> {
        T process(T input);
        List<? extends T> getList();
        void setList(List<? super T> list);
    }

    public static class VariantImplementation<T extends Number>
        implements VariantInterface<T> {

        @Override
        public T process(T input) {
            return input;
        }

        @Override
        public List<? extends T> getList() {
            return new ArrayList<>();
        }

        @Override
        public void setList(List<? super T> list) {
            // Implementation
        }
    }

    // Variance capture scenarios
    public void captureScenarios() {
        List<?> unknownList = new ArrayList<String>();

        // Capture of ?
        captureHelper(unknownList);

        // Variance with captured types
        List<? extends Number> numberList = new ArrayList<Integer>();
        processNumberList(numberList);
    }

    private <T> void captureHelper(List<T> list) {
        // T is captured from ?
        if (!list.isEmpty()) {
            T item = list.get(0);
            list.add(item);
        }
    }

    private void processNumberList(List<? extends Number> list) {
        // Can read but not write
        for (Number num : list) {
            System.out.println(num);
        }
    }

    // PECS (Producer Extends, Consumer Super) examples
    public static <T> void copy(
        List<? extends T> source,    // Producer - use extends
        List<? super T> destination  // Consumer - use super
    ) {
        for (T item : source) {
            destination.add(item);
        }
    }

    // Complex PECS scenario
    public <T extends Comparable<? super T>> void sort(List<T> list) {
        Collections.sort(list);
    }

    // Variance with enum types
    private List<? extends Enum<?>> enumList;
    private Set<? super Enum<? extends Enum<?>>> complexEnumVariance;

    // Variance with annotation types
    private Class<? extends java.lang.annotation.Annotation> annotationClass;
    private List<? super java.lang.annotation.Annotation> annotationList;

    // Extreme variance nesting
    private Map<
        ? extends String,
        ? extends List<
            ? extends Set<
                ? extends Map<
                    ? super Integer,
                    ? super List<? super String>
                >
            >
        >
    > extremeVarianceNesting;

    // Variance with type intersections
    public <T extends Serializable & Comparable<T>> void intersectionVariance(
        List<? extends T> input
    ) {
        // Implementation
    }

    // Variance with raw types (legacy)
    @SuppressWarnings({"unchecked", "rawtypes"})
    public void rawTypeVariance() {
        List rawList = new ArrayList();
        List<? extends Object> wildcardList = rawList;
        List<String> stringList = rawList; // Unchecked
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify generic variance edge cases are parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    {call[0][1]["qualified_name"] for call in interface_calls}

    # Should detect classes with complex generic variance
    assert any("GenericVarianceEdgeCases" in qn for qn in created_classes)
    assert len(created_classes) >= 3, "Should detect multiple classes with variance"


def test_annotation_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of annotation edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AnnotationEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example;

import java.lang.annotation.*;
import java.util.*;

// Multiple annotations on class
@Deprecated
@SuppressWarnings("all")
@CustomAnnotation
public class AnnotationEdgeCases {

    // Multiple annotations on field
    @Deprecated
    @SuppressWarnings({"unchecked", "rawtypes"})
    @CustomAnnotation
    private String multiAnnotatedField;

    // Annotation with various value types
    @ComplexAnnotation(
        stringValue = "test",
        intValue = 42,
        doubleValue = 3.14,
        booleanValue = true,
        enumValue = TestEnum.VALUE1,
        classValue = String.class,
        annotationValue = @SimpleAnnotation("nested"),
        stringArray = {"a", "b", "c"},
        intArray = {1, 2, 3},
        enumArray = {TestEnum.VALUE1, TestEnum.VALUE2}
    )
    private String complexAnnotatedField;

    // Annotation without parentheses
    @Deprecated
    @Override
    public String toString() {
        return "AnnotationEdgeCases";
    }

    // Annotation with single value
    @SuppressWarnings("unchecked")
    public void singleValueAnnotation() {}

    // Annotation with array as single value
    @SuppressWarnings({"unchecked", "rawtypes"})
    public void arrayValueAnnotation() {}

    // Multiple annotations on method parameters
    public void parameterAnnotations(
        @Deprecated @CustomAnnotation String param1,
        @SuppressWarnings("unused") int param2,
        @ComplexAnnotation(stringValue = "param") String param3
    ) {}

    // Annotations on type parameters
    public <@CustomAnnotation T extends @Deprecated String> void typeParameterAnnotations(T param) {}

    // Annotations in type arguments
    public List<@CustomAnnotation String> annotatedTypeArguments() {
        return new ArrayList<@CustomAnnotation String>();
    }

    // Annotations on array types
    public @CustomAnnotation String @Deprecated [] annotatedArrayType() {
        return new String[0];
    }

    // Multiple dimensions with annotations
    public @CustomAnnotation String @Deprecated [] @SimpleAnnotation("2d") [] multiDimAnnotatedArray() {
        return new String[0][0];
    }

    // Annotations on local variables
    public void localVariableAnnotations() {
        @SuppressWarnings("unused")
        @Deprecated
        String localVar = "test";

        @CustomAnnotation
        final int finalLocal = 42;
    }

    // Annotations on constructors
    @Deprecated
    @SuppressWarnings("unused")
    public AnnotationEdgeCases(@CustomAnnotation String param) {}

    // Nested annotations
    @OuterAnnotation(
        inner = @InnerAnnotation(value = "nested"),
        inners = {
            @InnerAnnotation(value = "array1"),
            @InnerAnnotation(value = "array2")
        }
    )
    public void nestedAnnotations() {}

    // Annotation with class literals
    @ClassAnnotation({String.class, Integer.class, List.class})
    public void classLiteralAnnotation() {}

    // Annotation with enum constants
    @EnumAnnotation(TestEnum.VALUE2)
    public void enumAnnotation() {}

    // Marker annotation (no parameters)
    @Marker
    public void markerAnnotation() {}

    // Repeatable annotation usage
    @RepeatableAnnotation("first")
    @RepeatableAnnotation("second")
    @RepeatableAnnotation("third")
    public void repeatableAnnotations() {}

    // Annotation with default values used
    @DefaultValueAnnotation
    public void defaultValueUsage() {}

    // Annotation with some defaults overridden
    @DefaultValueAnnotation(name = "overridden")
    public void partialOverride() {}
}

// Simple annotation
@interface SimpleAnnotation {
    String value();
}

// Complex annotation with multiple types
@interface ComplexAnnotation {
    String stringValue() default "";
    int intValue() default 0;
    double doubleValue() default 0.0;
    boolean booleanValue() default false;
    TestEnum enumValue() default TestEnum.VALUE1;
    Class<?> classValue() default Object.class;
    SimpleAnnotation annotationValue() default @SimpleAnnotation("default");
    String[] stringArray() default {};
    int[] intArray() default {};
    TestEnum[] enumArray() default {};
}

// Marker annotation
@interface Marker {
}

// Annotation with default values
@interface DefaultValueAnnotation {
    String name() default "default";
    int priority() default 1;
    boolean enabled() default true;
}

// Custom annotation
@interface CustomAnnotation {
    String value() default "";
}

// Nested annotation structures
@interface OuterAnnotation {
    InnerAnnotation inner();
    InnerAnnotation[] inners() default {};
}

@interface InnerAnnotation {
    String value();
}

// Class literal annotation
@interface ClassAnnotation {
    Class<?>[] value();
}

// Enum annotation
@interface EnumAnnotation {
    TestEnum value();
}

// Repeatable annotation
@Repeatable(RepeatableContainer.class)
@interface RepeatableAnnotation {
    String value();
}

@interface RepeatableContainer {
    RepeatableAnnotation[] value();
}

// Test enum for annotations
enum TestEnum {
    VALUE1, VALUE2, VALUE3
}

// Meta-annotated annotation
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.TYPE, ElementType.METHOD, ElementType.FIELD})
@Documented
@interface MetaAnnotated {
    String description() default "";
}

// Annotation inheritance through interfaces
interface AnnotatedInterface {
    @Deprecated
    void interfaceMethod();
}

class ImplementingClass implements AnnotatedInterface {
    @Override
    public void interfaceMethod() {
        // Inherits @Deprecated from interface
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify annotation edge cases are parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}
    {call[0][1]["qualified_name"] for call in interface_calls}

    # Should detect main class, annotation interfaces, and enum
    assert any("AnnotationEdgeCases" in qn for qn in created_classes)
    assert len(created_classes) >= 3, "Should detect multiple classes and annotations"
    assert len(created_enums) >= 1, "Should detect enum"


def test_operator_and_expression_edge_cases(
    java_edge_cases_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parsing of complex operator and expression edge cases."""
    test_file = (
        java_edge_cases_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "OperatorEdgeCases.java"
    )
    test_file.write_text(
        """
package com.example;

public class OperatorEdgeCases {

    // Complex arithmetic expressions
    public void arithmeticComplexity() {
        int result1 = 1 + 2 * 3 - 4 / 5 % 6;
        int result2 = (1 + 2) * (3 - 4) / (5 % 6);
        int result3 = 1 + 2 * 3 - 4 / 5 % 6 + 7 * 8 - 9 / 10 % 11;

        // Unary operators
        int x = 5;
        int result4 = ++x + --x - x++ + x--;
        int result5 = +x - -x + ~x;
        boolean result6 = !true && !false || !true;
    }

    // Bitwise operations
    public void bitwiseOperations() {
        int a = 0b1010;
        int b = 0b1100;

        int and = a & b;
        int or = a | b;
        int xor = a ^ b;
        int not = ~a;
        int leftShift = a << 2;
        int rightShift = a >> 2;
        int unsignedRightShift = a >>> 2;

        // Complex bitwise expressions
        int complex = (a & b) | (a ^ b) & ~(a | b) << 1 >>> 2;
    }

    // Comparison chains
    public void comparisonChains() {
        int x = 5, y = 10, z = 15;

        boolean result1 = x < y && y < z;
        boolean result2 = x <= y && y <= z;
        boolean result3 = x != y && y != z && x != z;
        boolean result4 = (x < y) == (y < z);
        boolean result5 = x < y ? y < z : z < x;
    }

    // Ternary operator chains
    public void ternaryChains() {
        int x = 5, y = 10, z = 15;

        int result1 = x > y ? x : y > z ? y : z;
        int result2 = (x > y) ? (y > z ? y : z) : (x > z ? x : z);
        String result3 = x > 0 ? "positive" : x < 0 ? "negative" : "zero";

        // Nested ternary
        int result4 = x > 0 ?
                     (y > 0 ? 1 : -1) :
                     (y > 0 ? 2 : -2);
    }

    // String concatenation edge cases
    public void stringConcatenation() {
        String result1 = "a" + "b" + "c";
        String result2 = "number: " + 42 + " and " + 3.14;
        String result3 = 1 + 2 + "result" + 3 + 4;  // "3result34"
        String result4 = "result" + 1 + 2;           // "result12"
        String result5 = "result" + (1 + 2);         // "result3"

        // Complex concatenation
        String complex = "start" +
                        (true ? "middle" : "other") +
                        (5 > 3 ? "end" : "alternative") +
                        Math.random();
    }

    // Method chaining
    public void methodChaining() {
        String result = "test"
                       .toUpperCase()
                       .trim()
                       .substring(1)
                       .replace("E", "e")
                       .toLowerCase();

        // Complex chaining with generics
        java.util.List<String> list = java.util.Arrays.asList("a", "b", "c")
                                     .stream()
                                     .filter(s -> s.length() > 0)
                                     .map(String::toUpperCase)
                                     .collect(java.util.stream.Collectors.toList());
    }

    // instanceof chains
    public void instanceofChains(Object obj) {
        if (obj instanceof String && ((String) obj).length() > 0) {
            System.out.println("Non-empty string");
        }

        if (obj instanceof String s && s.length() > 0) {
            System.out.println("Pattern matching: " + s);
        }

        boolean complex = obj instanceof String ||
                         obj instanceof Integer ||
                         obj instanceof Double;
    }

    // Array access chains
    public void arrayAccessChains() {
        int[][] matrix = {{1, 2}, {3, 4}};
        int[][][] cube = {{{1, 2}, {3, 4}}, {{5, 6}, {7, 8}}};

        int value1 = matrix[0][1];
        int value2 = cube[0][1][0];

        // Complex array access
        int value3 = matrix[Math.min(0, 1)][Math.max(0, 1)];

        // Array access with method calls
        String[] strings = {"hello", "world"};
        char character = strings[0].charAt(0);
    }

    // Lambda expression edge cases
    public void lambdaExpressions() {
        // Simple lambda
        java.util.function.Predicate<String> simple = s -> s.length() > 0;

        // Multi-parameter lambda
        java.util.function.BiFunction<Integer, Integer, Integer> add = (a, b) -> a + b;

        // Lambda with block
        java.util.function.Function<String, String> complex = s -> {
            String trimmed = s.trim();
            return trimmed.toUpperCase();
        };

        // Nested lambda
        java.util.function.Function<Integer, java.util.function.Function<Integer, Integer>>
            curried = a -> b -> a + b;

        // Lambda with exception handling
        java.util.function.Function<String, Integer> parseWithDefault = s -> {
            try {
                return Integer.parseInt(s);
            } catch (NumberFormatException e) {
                return 0;
            }
        };
    }

    // Method reference edge cases
    public void methodReferences() {
        // Static method reference
        java.util.function.Function<String, Integer> parseInt = Integer::parseInt;

        // Instance method reference
        java.util.function.Supplier<String> toString = this::toString;

        // Constructor reference
        java.util.function.Supplier<java.util.ArrayList<String>> constructor =
            java.util.ArrayList::new;

        // Array constructor reference
        java.util.function.IntFunction<String[]> arrayConstructor = String[]::new;
    }

    // Switch expression edge cases
    public void switchExpressions() {
        int dayOfWeek = 1;

        // Simple switch expression
        String dayName = switch (dayOfWeek) {
            case 1 -> "Monday";
            case 2 -> "Tuesday";
            case 3 -> "Wednesday";
            default -> "Other";
        };

        // Switch with yield
        int quarter = switch (dayOfWeek) {
            case 1, 2, 3 -> {
                System.out.println("Q1");
                yield 1;
            }
            case 4, 5, 6 -> {
                System.out.println("Q2");
                yield 2;
            }
            default -> {
                System.out.println("Other");
                yield 0;
            }
        };

        // Nested switch
        String result = switch (dayOfWeek) {
            case 1 -> switch (quarter) {
                case 1 -> "Monday Q1";
                default -> "Monday Other";
            };
            default -> "Not Monday";
        };
    }

    // Complex expression combinations
    public void complexExpressions() {
        int x = 5, y = 10;

        // Combination of multiple operators
        boolean complex1 = (x++ > 5) && (--y < 10) || (x * y > 50);

        // Expression with method calls and operators
        boolean complex2 = Math.abs(x - y) > 2 &&
                          String.valueOf(x).length() == String.valueOf(y).length();

        // Nested conditional with arithmetic
        int complex3 = x > y ?
                      (x * 2 + y * 3) :
                      (y * 2 - x * 3) +
                      (x > 0 ? 10 : -10);

        // Mixed operators with parentheses
        double complex4 = (Math.sqrt(x * x + y * y) + Math.abs(x - y)) /
                         (Math.max(x, y) - Math.min(x, y) + 1);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_edge_cases_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify complex operators and expressions are parsed
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    assert any("OperatorEdgeCases" in qn for qn in created_classes)
