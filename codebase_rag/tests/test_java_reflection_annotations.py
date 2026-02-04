from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, run_updater


@pytest.fixture
def java_reflection_project(temp_repo: Path) -> Path:
    """Create a Java project for testing reflection and annotations."""
    project_path = temp_repo / "java_reflection_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_custom_annotations(
    java_reflection_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom annotation definitions and usage."""
    test_file = (
        java_reflection_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CustomAnnotations.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.lang.annotation.*;

// Basic annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Test {
    String value() default "";
    int timeout() default 0;
    Class<? extends Exception>[] expected() default {};
}

// Marker annotation (no elements)
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.TYPE, ElementType.METHOD})
public @interface Deprecated {
}

// Annotation with various element types
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
public @interface Entity {
    String name() default "";
    String[] tables() default {};
    boolean readOnly() default false;
    int priority() default 0;
    Class<?> converter() default Object.class;
    Status status() default Status.ACTIVE;

    enum Status {
        ACTIVE, INACTIVE, PENDING
    }
}

// Meta-annotation (annotation on annotation)
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.ANNOTATION_TYPE)
public @interface MetaAnnotation {
    String description();
    String version() default "1.0";
}

// Annotation using meta-annotation
@MetaAnnotation(description = "Validation annotation", version = "2.0")
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.FIELD, ElementType.PARAMETER})
public @interface Validate {
    String pattern() default "";
    int min() default 0;
    int max() default Integer.MAX_VALUE;
    boolean required() default true;
    String message() default "Validation failed";
}

// Repeatable annotation (Java 8+)
@Repeatable(Schedules.class)
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Schedule {
    String cron();
    String zone() default "UTC";
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Schedules {
    Schedule[] value();
}

// Annotation with nested annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
public @interface Configuration {
    Property[] properties() default {};

    @interface Property {
        String key();
        String value();
        boolean encrypted() default false;
    }
}

// Different retention policies
@Retention(RetentionPolicy.SOURCE)
@Target(ElementType.METHOD)
public @interface SourceOnly {
    String value();
}

@Retention(RetentionPolicy.CLASS)
@Target(ElementType.TYPE)
public @interface ClassLevel {
    String description();
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.FIELD)
public @interface RuntimeVisible {
    String name();
}

// Complex annotation with all features
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.TYPE, ElementType.METHOD, ElementType.FIELD, ElementType.PARAMETER})
@Documented
@Inherited
public @interface ComplexAnnotation {
    // Primitive types
    boolean boolValue() default true;
    byte byteValue() default 0;
    short shortValue() default 0;
    int intValue() default 0;
    long longValue() default 0L;
    float floatValue() default 0.0f;
    double doubleValue() default 0.0;
    char charValue() default '\\0';

    // String
    String stringValue() default "";

    // Class
    Class<?> classValue() default Object.class;

    // Enum
    Priority priority() default Priority.NORMAL;

    // Arrays
    String[] stringArray() default {};
    int[] intArray() default {};
    Class<?>[] classArray() default {};
    Priority[] priorityArray() default {};

    // Nested annotation
    NestedAnnotation nested() default @NestedAnnotation;
    NestedAnnotation[] nestedArray() default {};

    enum Priority {
        LOW, NORMAL, HIGH, CRITICAL
    }

    @interface NestedAnnotation {
        String value() default "nested";
        int number() default 42;
    }
}

// Example class using annotations
@Entity(name = "user", tables = {"users", "user_profiles"}, priority = 5)
@Configuration(properties = {
    @Configuration.Property(key = "timeout", value = "30"),
    @Configuration.Property(key = "retries", value = "3", encrypted = true)
})
@ClassLevel(description = "User management class")
public class AnnotatedUser {

    @RuntimeVisible(name = "user_id")
    @Validate(min = 1, required = true, message = "ID must be positive")
    private Long id;

    @Validate(pattern = "[a-zA-Z]+", min = 2, max = 50, message = "Invalid name")
    private String name;

    @Validate(pattern = "\\\\S+@\\\\S+\\\\.\\\\S+", required = true, message = "Invalid email")
    private String email;

    @Test(value = "Simple test", timeout = 5000)
    public void simpleTest() {
        System.out.println("Simple test method");
    }

    @Test(value = "Exception test", expected = {IllegalArgumentException.class, RuntimeException.class})
    public void exceptionTest() throws Exception {
        throw new IllegalArgumentException("Test exception");
    }

    @Schedule(cron = "0 0 * * *", zone = "America/New_York")
    @Schedule(cron = "0 12 * * *", zone = "Europe/London")
    public void scheduledTask() {
        System.out.println("Scheduled task execution");
    }

    @Deprecated
    @SourceOnly("This method will be removed")
    public void deprecatedMethod() {
        System.out.println("Deprecated functionality");
    }

    @ComplexAnnotation(
        boolValue = false,
        intValue = 42,
        stringValue = "complex",
        classValue = String.class,
        priority = ComplexAnnotation.Priority.HIGH,
        stringArray = {"a", "b", "c"},
        intArray = {1, 2, 3},
        nested = @ComplexAnnotation.NestedAnnotation(value = "custom", number = 100),
        nestedArray = {
            @ComplexAnnotation.NestedAnnotation(value = "first"),
            @ComplexAnnotation.NestedAnnotation(value = "second", number = 200)
        }
    )
    public void complexAnnotatedMethod(@Validate(required = true) String parameter) {
        System.out.println("Complex annotation example: " + parameter);
    }
}
""",
    )

    run_updater(java_reflection_project, mock_ingestor, skip_if_missing="java")

    project_name = java_reflection_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Test",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Deprecated",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Entity",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.MetaAnnotation",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Validate",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Schedule",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Schedules",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.Configuration",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.SourceOnly",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.ClassLevel",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.RuntimeVisible",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.ComplexAnnotation",
        f"{project_name}.src.main.java.com.example.CustomAnnotations.AnnotatedUser",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected annotations/classes: {sorted(list(missing_classes))}"
    )


def test_reflection_api_usage(
    java_reflection_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java reflection API usage patterns."""
    test_file = (
        java_reflection_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ReflectionExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.lang.reflect.*;
import java.util.*;

public class ReflectionExample {

    // Class introspection
    public void inspectClass(Class<?> clazz) {
        System.out.println("Class: " + clazz.getName());
        System.out.println("Package: " + clazz.getPackage().getName());
        System.out.println("Modifiers: " + Modifier.toString(clazz.getModifiers()));
        System.out.println("Is interface: " + clazz.isInterface());
        System.out.println("Is enum: " + clazz.isEnum());
        System.out.println("Is annotation: " + clazz.isAnnotation());
        System.out.println("Is array: " + clazz.isArray());

        // Superclass and interfaces
        Class<?> superclass = clazz.getSuperclass();
        if (superclass != null) {
            System.out.println("Superclass: " + superclass.getName());
        }

        Class<?>[] interfaces = clazz.getInterfaces();
        System.out.println("Interfaces: " + Arrays.toString(interfaces));

        // Generic information
        TypeVariable<?>[] typeParameters = clazz.getTypeParameters();
        for (TypeVariable<?> typeParam : typeParameters) {
            System.out.println("Type parameter: " + typeParam.getName());
        }
    }

    // Field introspection
    public void inspectFields(Class<?> clazz) {
        System.out.println("\\nFields of " + clazz.getName() + ":");

        // Public fields only
        Field[] publicFields = clazz.getFields();
        for (Field field : publicFields) {
            printFieldInfo(field);
        }

        // All declared fields
        Field[] declaredFields = clazz.getDeclaredFields();
        for (Field field : declaredFields) {
            printFieldInfo(field);
        }
    }

    private void printFieldInfo(Field field) {
        System.out.println("  Field: " + field.getName());
        System.out.println("    Type: " + field.getType().getName());
        System.out.println("    Generic type: " + field.getGenericType());
        System.out.println("    Modifiers: " + Modifier.toString(field.getModifiers()));
        System.out.println("    Annotations: " + Arrays.toString(field.getAnnotations()));
    }

    // Method introspection
    public void inspectMethods(Class<?> clazz) {
        System.out.println("\\nMethods of " + clazz.getName() + ":");

        // Public methods (including inherited)
        Method[] publicMethods = clazz.getMethods();
        for (Method method : publicMethods) {
            if (method.getDeclaringClass() == clazz) { // Only declared methods
                printMethodInfo(method);
            }
        }

        // All declared methods
        Method[] declaredMethods = clazz.getDeclaredMethods();
        for (Method method : declaredMethods) {
            printMethodInfo(method);
        }
    }

    private void printMethodInfo(Method method) {
        System.out.println("  Method: " + method.getName());
        System.out.println("    Return type: " + method.getReturnType().getName());
        System.out.println("    Generic return type: " + method.getGenericReturnType());
        System.out.println("    Parameter types: " + Arrays.toString(method.getParameterTypes()));
        System.out.println("    Generic parameter types: " + Arrays.toString(method.getGenericParameterTypes()));
        System.out.println("    Exception types: " + Arrays.toString(method.getExceptionTypes()));
        System.out.println("    Modifiers: " + Modifier.toString(method.getModifiers()));
        System.out.println("    Annotations: " + Arrays.toString(method.getAnnotations()));

        // Parameter annotations
        Annotation[][] paramAnnotations = method.getParameterAnnotations();
        for (int i = 0; i < paramAnnotations.length; i++) {
            System.out.println("    Parameter " + i + " annotations: " + Arrays.toString(paramAnnotations[i]));
        }
    }

    // Constructor introspection
    public void inspectConstructors(Class<?> clazz) {
        System.out.println("\\nConstructors of " + clazz.getName() + ":");

        Constructor<?>[] constructors = clazz.getDeclaredConstructors();
        for (Constructor<?> constructor : constructors) {
            System.out.println("  Constructor: " + constructor.getName());
            System.out.println("    Parameter types: " + Arrays.toString(constructor.getParameterTypes()));
            System.out.println("    Generic parameter types: " + Arrays.toString(constructor.getGenericParameterTypes()));
            System.out.println("    Exception types: " + Arrays.toString(constructor.getExceptionTypes()));
            System.out.println("    Modifiers: " + Modifier.toString(constructor.getModifiers()));
            System.out.println("    Annotations: " + Arrays.toString(constructor.getAnnotations()));
        }
    }

    // Dynamic field access
    public Object getFieldValue(Object obj, String fieldName) throws Exception {
        Class<?> clazz = obj.getClass();
        Field field = clazz.getDeclaredField(fieldName);
        field.setAccessible(true); // Access private fields
        return field.get(obj);
    }

    public void setFieldValue(Object obj, String fieldName, Object value) throws Exception {
        Class<?> clazz = obj.getClass();
        Field field = clazz.getDeclaredField(fieldName);
        field.setAccessible(true);
        field.set(obj, value);
    }

    // Dynamic method invocation
    public Object invokeMethod(Object obj, String methodName, Class<?>[] paramTypes, Object... args) throws Exception {
        Class<?> clazz = obj.getClass();
        Method method = clazz.getDeclaredMethod(methodName, paramTypes);
        method.setAccessible(true);
        return method.invoke(obj, args);
    }

    // Dynamic object creation
    public Object createInstance(Class<?> clazz) throws Exception {
        return clazz.getDeclaredConstructor().newInstance();
    }

    public Object createInstance(Class<?> clazz, Class<?>[] paramTypes, Object... args) throws Exception {
        Constructor<?> constructor = clazz.getDeclaredConstructor(paramTypes);
        constructor.setAccessible(true);
        return constructor.newInstance(args);
    }

    // Array reflection
    public void inspectArray(Object array) {
        Class<?> clazz = array.getClass();
        if (clazz.isArray()) {
            System.out.println("Array component type: " + clazz.getComponentType().getName());
            System.out.println("Array length: " + Array.getLength(array));

            for (int i = 0; i < Array.getLength(array); i++) {
                Object element = Array.get(array, i);
                System.out.println("Element " + i + ": " + element);
            }
        }
    }

    public Object createArray(Class<?> componentType, int length) {
        return Array.newInstance(componentType, length);
    }

    // Generic type introspection
    public void inspectGenericTypes(Class<?> clazz) {
        System.out.println("\\nGeneric types of " + clazz.getName() + ":");

        Type genericSuperclass = clazz.getGenericSuperclass();
        if (genericSuperclass instanceof ParameterizedType) {
            ParameterizedType paramType = (ParameterizedType) genericSuperclass;
            Type[] actualTypes = paramType.getActualTypeArguments();
            System.out.println("Generic superclass arguments: " + Arrays.toString(actualTypes));
        }

        Type[] genericInterfaces = clazz.getGenericInterfaces();
        for (Type genericInterface : genericInterfaces) {
            if (genericInterface instanceof ParameterizedType) {
                ParameterizedType paramType = (ParameterizedType) genericInterface;
                Type[] actualTypes = paramType.getActualTypeArguments();
                System.out.println("Generic interface arguments: " + Arrays.toString(actualTypes));
            }
        }
    }

    // Annotation processing
    public void processAnnotations(Class<?> clazz) {
        System.out.println("\\nAnnotations of " + clazz.getName() + ":");

        Annotation[] annotations = clazz.getAnnotations();
        for (Annotation annotation : annotations) {
            System.out.println("Class annotation: " + annotation);
            processAnnotationValues(annotation);
        }

        // Process field annotations
        for (Field field : clazz.getDeclaredFields()) {
            Annotation[] fieldAnnotations = field.getAnnotations();
            if (fieldAnnotations.length > 0) {
                System.out.println("Field " + field.getName() + " annotations:");
                for (Annotation annotation : fieldAnnotations) {
                    System.out.println("  " + annotation);
                    processAnnotationValues(annotation);
                }
            }
        }

        // Process method annotations
        for (Method method : clazz.getDeclaredMethods()) {
            Annotation[] methodAnnotations = method.getAnnotations();
            if (methodAnnotations.length > 0) {
                System.out.println("Method " + method.getName() + " annotations:");
                for (Annotation annotation : methodAnnotations) {
                    System.out.println("  " + annotation);
                    processAnnotationValues(annotation);
                }
            }
        }
    }

    private void processAnnotationValues(Annotation annotation) {
        Class<? extends Annotation> annotationType = annotation.annotationType();
        Method[] methods = annotationType.getDeclaredMethods();

        for (Method method : methods) {
            try {
                Object value = method.invoke(annotation);
                System.out.println("    " + method.getName() + " = " + value);
            } catch (Exception e) {
                System.err.println("Error reading annotation value: " + e.getMessage());
            }
        }
    }

    // Class loading
    public Class<?> loadClass(String className) throws ClassNotFoundException {
        return Class.forName(className);
    }

    public Class<?> loadClass(String className, ClassLoader classLoader) throws ClassNotFoundException {
        return Class.forName(className, true, classLoader);
    }

    // Proxy creation
    public Object createProxy(Class<?>[] interfaces, InvocationHandler handler) {
        return Proxy.newProxyInstance(
            getClass().getClassLoader(),
            interfaces,
            handler
        );
    }

    // Example invocation handler
    public static class LoggingInvocationHandler implements InvocationHandler {
        private final Object target;

        public LoggingInvocationHandler(Object target) {
            this.target = target;
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            System.out.println("Before method: " + method.getName());
            try {
                Object result = method.invoke(target, args);
                System.out.println("After method: " + method.getName());
                return result;
            } catch (InvocationTargetException e) {
                System.out.println("Exception in method: " + method.getName());
                throw e.getCause();
            }
        }
    }
}
""",
    )

    run_updater(java_reflection_project, mock_ingestor, skip_if_missing="java")

    project_name = java_reflection_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.ReflectionExample.ReflectionExample",
        f"{project_name}.src.main.java.com.example.ReflectionExample.ReflectionExample.LoggingInvocationHandler",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_annotation_processing(
    java_reflection_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test annotation processing patterns."""
    test_file = (
        java_reflection_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AnnotationProcessor.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.lang.annotation.*;
import java.lang.reflect.*;
import java.util.*;

// Annotation for validation
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.FIELD, ElementType.PARAMETER})
@interface NotNull {
    String message() default "Field cannot be null";
}

@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.FIELD, ElementType.PARAMETER})
@interface Size {
    int min() default 0;
    int max() default Integer.MAX_VALUE;
    String message() default "Size validation failed";
}

@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.FIELD, ElementType.PARAMETER})
@interface Pattern {
    String value();
    String message() default "Pattern validation failed";
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
@interface Timed {
    boolean enabled() default true;
    String description() default "";
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
@interface Cacheable {
    String key() default "";
    int ttl() default 300; // seconds
}

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface Component {
    String value() default "";
    String scope() default "singleton";
}

@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.FIELD, ElementType.METHOD, ElementType.PARAMETER})
@interface Inject {
    String qualifier() default "";
    boolean required() default true;
}

// Example entity with validation annotations
@Component("userService")
class User {
    @NotNull(message = "User ID is required")
    private Long id;

    @NotNull(message = "Name is required")
    @Size(min = 2, max = 50, message = "Name must be between 2 and 50 characters")
    private String name;

    @NotNull(message = "Email is required")
    @Pattern(value = "\\\\S+@\\\\S+\\\\.\\\\S+", message = "Invalid email format")
    private String email;

    @Size(min = 8, max = 100, message = "Password must be between 8 and 100 characters")
    private String password;

    public User() {}

    public User(@NotNull Long id, @NotNull @Size(min = 2, max = 50) String name) {
        this.id = id;
        this.name = name;
    }

    // Getters and setters with validation
    public void setId(@NotNull Long id) {
        this.id = id;
    }

    public void setName(@NotNull @Size(min = 2, max = 50) String name) {
        this.name = name;
    }

    public void setEmail(@NotNull @Pattern(value = "\\\\S+@\\\\S+\\\\.\\\\S+") String email) {
        this.email = email;
    }

    public Long getId() { return id; }
    public String getName() { return name; }
    public String getEmail() { return email; }
}

// Service class with method-level annotations
@Component("userRepository")
class UserRepository {

    @Inject(qualifier = "dataSource")
    private Object dataSource;

    @Timed(description = "Find user by ID")
    @Cacheable(key = "user_#{id}", ttl = 600)
    public User findById(@NotNull Long id) {
        // Simulate database lookup
        return new User(id, "User " + id);
    }

    @Timed(description = "Find users by name pattern")
    @Cacheable(key = "users_#{pattern}", ttl = 300)
    public List<User> findByNamePattern(@NotNull @Pattern(value = "[a-zA-Z%]+") String pattern) {
        // Simulate database query
        return Arrays.asList(new User(1L, "John"), new User(2L, "Jane"));
    }

    @Timed(description = "Save user", enabled = true)
    public void save(@NotNull User user) {
        // Validate user before saving
        validateObject(user);
        // Simulate database save
    }

    private void validateObject(Object obj) {
        ValidationProcessor.validate(obj);
    }
}

// Annotation processor for validation
public class AnnotationProcessor {

    // Validation processor
    public static class ValidationProcessor {

        public static void validate(Object obj) {
            if (obj == null) {
                throw new IllegalArgumentException("Object cannot be null");
            }

            Class<?> clazz = obj.getClass();
            validateFields(obj, clazz);
        }

        private static void validateFields(Object obj, Class<?> clazz) {
            for (Field field : clazz.getDeclaredFields()) {
                field.setAccessible(true);

                try {
                    Object value = field.get(obj);
                    validateField(field, value);
                } catch (IllegalAccessException e) {
                    throw new RuntimeException("Cannot access field: " + field.getName(), e);
                }
            }
        }

        private static void validateField(Field field, Object value) {
            // Check @NotNull
            if (field.isAnnotationPresent(NotNull.class)) {
                if (value == null) {
                    NotNull annotation = field.getAnnotation(NotNull.class);
                    throw new ValidationException(annotation.message());
                }
            }

            // Check @Size
            if (field.isAnnotationPresent(Size.class) && value != null) {
                Size annotation = field.getAnnotation(Size.class);
                int length = getLength(value);
                if (length < annotation.min() || length > annotation.max()) {
                    throw new ValidationException(annotation.message());
                }
            }

            // Check @Pattern
            if (field.isAnnotationPresent(Pattern.class) && value instanceof String) {
                Pattern annotation = field.getAnnotation(Pattern.class);
                String stringValue = (String) value;
                if (!stringValue.matches(annotation.value())) {
                    throw new ValidationException(annotation.message());
                }
            }
        }

        private static int getLength(Object value) {
            if (value instanceof String) {
                return ((String) value).length();
            } else if (value instanceof Collection) {
                return ((Collection<?>) value).size();
            } else if (value.getClass().isArray()) {
                return Array.getLength(value);
            }
            return 0;
        }

        public static void validateMethodParameters(Method method, Object[] args) {
            Annotation[][] paramAnnotations = method.getParameterAnnotations();

            for (int i = 0; i < paramAnnotations.length; i++) {
                Object arg = args[i];
                Annotation[] annotations = paramAnnotations[i];

                for (Annotation annotation : annotations) {
                    validateParameterAnnotation(annotation, arg);
                }
            }
        }

        private static void validateParameterAnnotation(Annotation annotation, Object value) {
            if (annotation instanceof NotNull) {
                if (value == null) {
                    NotNull notNull = (NotNull) annotation;
                    throw new ValidationException(notNull.message());
                }
            } else if (annotation instanceof Size && value != null) {
                Size size = (Size) annotation;
                int length = getLength(value);
                if (length < size.min() || length > size.max()) {
                    throw new ValidationException(size.message());
                }
            } else if (annotation instanceof Pattern && value instanceof String) {
                Pattern pattern = (Pattern) annotation;
                String stringValue = (String) value;
                if (!stringValue.matches(pattern.value())) {
                    throw new ValidationException(pattern.message());
                }
            }
        }
    }

    // Component processor for dependency injection
    public static class ComponentProcessor {

        private static final Map<String, Object> components = new HashMap<>();

        public static void registerComponent(Object component) {
            Class<?> clazz = component.getClass();
            if (clazz.isAnnotationPresent(Component.class)) {
                Component annotation = clazz.getAnnotation(Component.class);
                String name = annotation.value().isEmpty() ? clazz.getSimpleName() : annotation.value();
                components.put(name, component);
            }
        }

        public static void injectDependencies(Object obj) {
            Class<?> clazz = obj.getClass();

            // Inject into fields
            for (Field field : clazz.getDeclaredFields()) {
                if (field.isAnnotationPresent(Inject.class)) {
                    field.setAccessible(true);

                    Inject inject = field.getAnnotation(Inject.class);
                    String qualifier = inject.qualifier();
                    Object dependency = components.get(qualifier);

                    if (dependency == null && inject.required()) {
                        throw new RuntimeException("Required dependency not found: " + qualifier);
                    }

                    try {
                        field.set(obj, dependency);
                    } catch (IllegalAccessException e) {
                        throw new RuntimeException("Cannot inject dependency", e);
                    }
                }
            }
        }

        public static <T> T getComponent(String name, Class<T> type) {
            Object component = components.get(name);
            if (component != null && type.isInstance(component)) {
                return type.cast(component);
            }
            return null;
        }
    }

    // Method interceptor for @Timed and @Cacheable
    public static class MethodInterceptor implements InvocationHandler {

        private final Object target;
        private final Map<String, Object> cache = new HashMap<>();

        public MethodInterceptor(Object target) {
            this.target = target;
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            // Validate parameters
            ValidationProcessor.validateMethodParameters(method, args);

            // Check cache
            if (method.isAnnotationPresent(Cacheable.class)) {
                Cacheable cacheable = method.getAnnotation(Cacheable.class);
                String cacheKey = generateCacheKey(cacheable.key(), method, args);

                if (cache.containsKey(cacheKey)) {
                    return cache.get(cacheKey);
                }
            }

            // Time execution
            long startTime = 0;
            if (method.isAnnotationPresent(Timed.class)) {
                Timed timed = method.getAnnotation(Timed.class);
                if (timed.enabled()) {
                    startTime = System.currentTimeMillis();
                }
            }

            try {
                Object result = method.invoke(target, args);

                // Cache result
                if (method.isAnnotationPresent(Cacheable.class)) {
                    Cacheable cacheable = method.getAnnotation(Cacheable.class);
                    String cacheKey = generateCacheKey(cacheable.key(), method, args);
                    cache.put(cacheKey, result);
                }

                return result;

            } finally {
                // Log timing
                if (method.isAnnotationPresent(Timed.class)) {
                    Timed timed = method.getAnnotation(Timed.class);
                    if (timed.enabled()) {
                        long duration = System.currentTimeMillis() - startTime;
                        System.out.println("Method " + method.getName() + " took " + duration + "ms" +
                            (timed.description().isEmpty() ? "" : " (" + timed.description() + ")"));
                    }
                }
            }
        }

        private String generateCacheKey(String keyTemplate, Method method, Object[] args) {
            if (keyTemplate.isEmpty()) {
                return method.getName() + Arrays.toString(args);
            }

            String key = keyTemplate;
            for (int i = 0; i < args.length; i++) {
                key = key.replace("#{" + i + "}", String.valueOf(args[i]));
            }
            return key;
        }
    }

    // Custom validation exception
    public static class ValidationException extends RuntimeException {
        public ValidationException(String message) {
            super(message);
        }
    }
}
""",
    )

    run_updater(java_reflection_project, mock_ingestor, skip_if_missing="java")

    project_name = java_reflection_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.User",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.UserRepository",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.AnnotationProcessor",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.AnnotationProcessor.ValidationProcessor",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.AnnotationProcessor.ComponentProcessor",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.AnnotationProcessor.MethodInterceptor",
        f"{project_name}.src.main.java.com.example.AnnotationProcessor.AnnotationProcessor.ValidationException",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_meta_annotations_inheritance(
    java_reflection_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test meta-annotations and annotation inheritance patterns."""
    test_file = (
        java_reflection_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MetaAnnotations.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.lang.annotation.*;

// Base meta-annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.ANNOTATION_TYPE)
@Documented
@interface FrameworkAnnotation {
    String framework();
    String version() default "1.0";
    String[] authors() default {};
}

// Meta-annotation for service layers
@FrameworkAnnotation(framework = "ServiceFramework", version = "2.0", authors = {"Dev Team"})
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@Documented
@Inherited
@interface ServiceLayer {
    String value() default "";
    String description() default "";
    boolean transactional() default false;
}

// Meta-annotation for persistence
@FrameworkAnnotation(framework = "PersistenceFramework", version = "3.0")
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@Documented
@interface PersistenceLayer {
    String dataSource() default "default";
    boolean readOnly() default false;
}

// Composed annotation combining multiple concerns
@ServiceLayer(transactional = true)
@PersistenceLayer
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface Repository {
    String value() default "";
    String dataSource() default "primary";
    boolean cacheable() default true;
}

// Method-level composed annotation
@FrameworkAnnotation(framework = "SecurityFramework")
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
@interface Secured {
    String[] roles() default {};
    String permission() default "";
    boolean audit() default true;
}

@FrameworkAnnotation(framework = "ValidationFramework")
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.METHOD, ElementType.PARAMETER})
@interface Validated {
    String[] groups() default {};
    boolean fast() default false;
}

// Composed method annotation
@Secured(audit = true)
@Validated(fast = false)
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
@interface AdminOperation {
    String[] requiredRoles() default {"admin"};
    String operation();
    boolean logged() default true;
}

// Conditional meta-annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.ANNOTATION_TYPE)
@interface ConditionalOn {
    String property();
    String value() default "true";
    boolean matchIfMissing() default false;
}

// Conditional service annotation
@ConditionalOn(property = "feature.userService.enabled", value = "true")
@ServiceLayer(transactional = true)
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface ConditionalUserService {
    String value() default "userService";
}

// Profile-based annotation
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface Profile {
    String[] value();
    boolean not() default false;
}

@Profile({"development", "testing"})
@ServiceLayer
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@interface DevTestService {
    String value() default "";
}

// Example classes using meta-annotations

@Repository(value = "userRepo", dataSource = "userDB", cacheable = true)
class UserRepository {

    @AdminOperation(operation = "deleteAllUsers", requiredRoles = {"admin", "superuser"}, logged = true)
    public void deleteAllUsers() {
        System.out.println("Deleting all users - admin operation");
    }

    @Secured(roles = {"user", "admin"}, permission = "read")
    @Validated(groups = {"basic"}, fast = true)
    public String findUser(String id) {
        return "User: " + id;
    }
}

@ConditionalUserService("conditionalUserService")
class ConditionalUserServiceImpl {

    public void processUser() {
        System.out.println("Processing user conditionally");
    }
}

@DevTestService("devTestService")
class DevelopmentTestService {

    public void debugOperation() {
        System.out.println("Debug operation for dev/test");
    }
}

// Multi-level inheritance
@ServiceLayer(description = "Base service", transactional = true)
abstract class BaseService {

    @Validated
    public abstract void process(String data);
}

@PersistenceLayer(dataSource = "analytics")
class AnalyticsService extends BaseService {

    @Override
    @Secured(roles = {"analyst"})
    public void process(String data) {
        System.out.println("Processing analytics data: " + data);
    }

    @AdminOperation(operation = "clearAnalytics", logged = true)
    public void clearAnalytics() {
        System.out.println("Clearing analytics data");
    }
}

// Meta-annotation processor
public class MetaAnnotations {

    public static void processMetaAnnotations(Class<?> clazz) {
        System.out.println("Processing meta-annotations for: " + clazz.getName());

        // Process class-level annotations
        Annotation[] annotations = clazz.getAnnotations();
        for (Annotation annotation : annotations) {
            processAnnotationHierarchy(annotation, 0);
        }

        // Process method-level annotations
        for (java.lang.reflect.Method method : clazz.getDeclaredMethods()) {
            if (method.getAnnotations().length > 0) {
                System.out.println("Method: " + method.getName());
                for (Annotation annotation : method.getAnnotations()) {
                    processAnnotationHierarchy(annotation, 1);
                }
            }
        }
    }

    private static void processAnnotationHierarchy(Annotation annotation, int depth) {
        String indent = "  ".repeat(depth);
        System.out.println(indent + "Annotation: " + annotation.annotationType().getSimpleName());

        // Check for meta-annotations
        Class<? extends Annotation> annotationType = annotation.annotationType();
        Annotation[] metaAnnotations = annotationType.getAnnotations();

        for (Annotation metaAnnotation : metaAnnotations) {
            // Skip built-in meta-annotations
            if (isCustomMetaAnnotation(metaAnnotation)) {
                processAnnotationHierarchy(metaAnnotation, depth + 1);
            }
        }

        // Process annotation values
        processAnnotationValues(annotation, depth + 1);
    }

    private static boolean isCustomMetaAnnotation(Annotation annotation) {
        String packageName = annotation.annotationType().getPackage().getName();
        return !packageName.startsWith("java.lang.annotation");
    }

    private static void processAnnotationValues(Annotation annotation, int depth) {
        String indent = "  ".repeat(depth);
        Class<? extends Annotation> annotationType = annotation.annotationType();

        for (java.lang.reflect.Method method : annotationType.getDeclaredMethods()) {
            try {
                Object value = method.invoke(annotation);
                System.out.println(indent + method.getName() + " = " + formatValue(value));
            } catch (Exception e) {
                // Ignore reflection errors
            }
        }
    }

    private static String formatValue(Object value) {
        if (value.getClass().isArray()) {
            return java.util.Arrays.toString((Object[]) value);
        }
        return String.valueOf(value);
    }
}
""",
    )

    run_updater(java_reflection_project, mock_ingestor, skip_if_missing="java")

    project_name = java_reflection_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.MetaAnnotations.UserRepository",
        f"{project_name}.src.main.java.com.example.MetaAnnotations.ConditionalUserServiceImpl",
        f"{project_name}.src.main.java.com.example.MetaAnnotations.DevelopmentTestService",
        f"{project_name}.src.main.java.com.example.MetaAnnotations.BaseService",
        f"{project_name}.src.main.java.com.example.MetaAnnotations.AnalyticsService",
        f"{project_name}.src.main.java.com.example.MetaAnnotations.MetaAnnotations",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
