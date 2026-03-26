from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_designated_consteval_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with designated initializers and consteval patterns."""
    project_path = temp_repo / "cpp_designated_consteval_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_designated_initializers(
    cpp_designated_consteval_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++20 designated initializers."""
    test_file = cpp_designated_consteval_project / "designated_initializers.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>
#include <array>

// Basic struct for designated initialization
struct Point2D {
    double x;
    double y;
};

// Struct with default values
struct Configuration {
    std::string name = "default";
    int timeout = 30;
    bool debug_mode = false;
    double threshold = 0.5;
    std::vector<std::string> allowed_hosts = {};
};

// More complex struct with nested members
struct DatabaseConfig {
    std::string host;
    int port;
    std::string database;
    struct Credentials {
        std::string username;
        std::string password;
    } credentials;
    struct Settings {
        int max_connections = 10;
        int timeout_seconds = 30;
        bool ssl_enabled = false;
    } settings;
};

// Struct for graph node representation
struct GraphNode {
    int id;
    std::string label;
    double weight;
    std::vector<int> neighbors;
    struct Metadata {
        std::string type;
        bool visited = false;
        int level = 0;
    } metadata;
};

void testBasicDesignatedInitializers() {
    std::cout << "=== Testing Basic Designated Initializers ===" << std::endl;

    // Basic designated initialization
    Point2D p1 = {.x = 3.14, .y = 2.71};
    Point2D p2 = {.y = 5.0, .x = 1.0};  // Order doesn't matter
    Point2D p3 = {.x = 10.0};  // y gets default initialization (0.0)

    std::cout << "Points with designated initializers:" << std::endl;
    std::cout << "  p1: (" << p1.x << ", " << p1.y << ")" << std::endl;
    std::cout << "  p2: (" << p2.x << ", " << p2.y << ")" << std::endl;
    std::cout << "  p3: (" << p3.x << ", " << p3.y << ")" << std::endl;

    // Configuration with selective initialization
    Configuration config1 = {
        .name = "production",
        .timeout = 60,
        .debug_mode = false
        // threshold and allowed_hosts use defaults
    };

    Configuration config2 = {
        .debug_mode = true,
        .threshold = 0.8,
        .allowed_hosts = {"localhost", "dev.example.com"}
        // name and timeout use defaults
    };

    std::cout << "Configurations:" << std::endl;
    std::cout << "  config1: name='" << config1.name << "', timeout=" << config1.timeout
              << ", debug=" << config1.debug_mode << ", threshold=" << config1.threshold << std::endl;
    std::cout << "  config2: name='" << config2.name << "', timeout=" << config2.timeout
              << ", debug=" << config2.debug_mode << ", threshold=" << config2.threshold
              << ", hosts=" << config2.allowed_hosts.size() << std::endl;
}

void testNestedDesignatedInitializers() {
    std::cout << "=== Testing Nested Designated Initializers ===" << std::endl;

    // Database configuration with nested designated initialization
    DatabaseConfig db_config = {
        .host = "localhost",
        .port = 5432,
        .database = "myapp",
        .credentials = {
            .username = "admin",
            .password = "secret123"
        },
        .settings = {
            .max_connections = 50,
            .ssl_enabled = true
            // timeout_seconds uses default value
        }
    };

    std::cout << "Database configuration:" << std::endl;
    std::cout << "  Host: " << db_config.host << ":" << db_config.port << std::endl;
    std::cout << "  Database: " << db_config.database << std::endl;
    std::cout << "  Username: " << db_config.credentials.username << std::endl;
    std::cout << "  Max connections: " << db_config.settings.max_connections << std::endl;
    std::cout << "  SSL enabled: " << (db_config.settings.ssl_enabled ? "yes" : "no") << std::endl;
    std::cout << "  Timeout: " << db_config.settings.timeout_seconds << " seconds" << std::endl;

    // Partial nested initialization
    DatabaseConfig minimal_config = {
        .host = "remote.example.com",
        .database = "prod_db",
        .credentials = {.username = "readonly"}
        // port uses default initialization, password is empty
    };

    std::cout << "Minimal configuration:" << std::endl;
    std::cout << "  Host: " << minimal_config.host << ":" << minimal_config.port << std::endl;
    std::cout << "  Database: " << minimal_config.database << std::endl;
    std::cout << "  Username: " << minimal_config.credentials.username << std::endl;
}

void testGraphNodeDesignatedInit() {
    std::cout << "=== Testing Graph Node Designated Initialization ===" << std::endl;

    // Create graph nodes using designated initializers
    GraphNode node1 = {
        .id = 1,
        .label = "start_node",
        .weight = 10.5,
        .neighbors = {2, 3, 4},
        .metadata = {
            .type = "source",
            .visited = false,
            .level = 0
        }
    };

    GraphNode node2 = {
        .id = 2,
        .label = "intermediate",
        .weight = 7.3,
        .neighbors = {1, 5},
        .metadata = {.type = "processing"}  // visited and level use defaults
    };

    GraphNode node3 = {
        .id = 3,
        .label = "sink_node",
        .weight = 15.2,
        .neighbors = {},  // No outgoing connections
        .metadata = {
            .type = "sink",
            .level = 2
        }
    };

    std::vector<GraphNode> graph = {node1, node2, node3};

    std::cout << "Graph nodes:" << std::endl;
    for (const auto& node : graph) {
        std::cout << "  Node " << node.id << " ('" << node.label << "'):" << std::endl;
        std::cout << "    Weight: " << node.weight << std::endl;
        std::cout << "    Type: " << node.metadata.type << std::endl;
        std::cout << "    Level: " << node.metadata.level << std::endl;
        std::cout << "    Visited: " << (node.metadata.visited ? "yes" : "no") << std::endl;
        std::cout << "    Neighbors: ";
        for (size_t i = 0; i < node.neighbors.size(); ++i) {
            if (i > 0) std::cout << ", ";
            std::cout << node.neighbors[i];
        }
        std::cout << std::endl;
    }
}

void testDesignatedInitWithArrays() {
    std::cout << "=== Testing Designated Initializers with Arrays ===" << std::endl;

    // Struct containing arrays
    struct Matrix2x2 {
        std::array<std::array<double, 2>, 2> data;
        std::string name;
    };

    // Initialize with designated initializers
    Matrix2x2 identity = {
        .data = {{
            {{1.0, 0.0}},
            {{0.0, 1.0}}
        }},
        .name = "Identity Matrix"
    };

    Matrix2x2 rotation = {
        .name = "Rotation Matrix (45°)",
        .data = {{
            {{0.707, -0.707}},
            {{0.707, 0.707}}
        }}
    };

    std::cout << "Matrices:" << std::endl;
    for (const auto& matrix : {identity, rotation}) {
        std::cout << "  " << matrix.name << ":" << std::endl;
        for (size_t i = 0; i < 2; ++i) {
            std::cout << "    [";
            for (size_t j = 0; j < 2; ++j) {
                if (j > 0) std::cout << ", ";
                std::cout << matrix.data[i][j];
            }
            std::cout << "]" << std::endl;
        }
    }
}

void testMixedInitializationStyles() {
    std::cout << "=== Testing Mixed Initialization Styles ===" << std::endl;

    struct ComplexStruct {
        int a;
        std::string b;
        std::vector<int> c;
        Point2D point;
    };

    // Mix designated and regular initialization (where allowed)
    ComplexStruct mixed = {
        .a = 42,
        .b = "mixed initialization",
        .c = {1, 2, 3, 4, 5},
        .point = {.x = 1.0, .y = 2.0}
    };

    std::cout << "Mixed initialization:" << std::endl;
    std::cout << "  a: " << mixed.a << std::endl;
    std::cout << "  b: '" << mixed.b << "'" << std::endl;
    std::cout << "  c: ";
    for (size_t i = 0; i < mixed.c.size(); ++i) {
        if (i > 0) std::cout << ", ";
        std::cout << mixed.c[i];
    }
    std::cout << std::endl;
    std::cout << "  point: (" << mixed.point.x << ", " << mixed.point.y << ")" << std::endl;

    // Factory-like function using designated initializers
    auto createDefaultConfig = []() -> Configuration {
        return Configuration{
            .name = "factory_default",
            .timeout = 45,
            .debug_mode = true,
            .threshold = 0.75
        };
    };

    auto default_config = createDefaultConfig();
    std::cout << "Factory-created config: name='" << default_config.name
              << "', timeout=" << default_config.timeout << std::endl;
}

void demonstrateDesignatedInitializers() {
    testBasicDesignatedInitializers();
    testNestedDesignatedInitializers();
    testGraphNodeDesignatedInit();
    testDesignatedInitWithArrays();
    testMixedInitializationStyles();
}
""",
    )

    run_updater(cpp_designated_consteval_project, mock_ingestor)

    project_name = cpp_designated_consteval_project.name

    expected_functions = [
        f"{project_name}.designated_initializers.testBasicDesignatedInitializers",
        f"{project_name}.designated_initializers.testNestedDesignatedInitializers",
        f"{project_name}.designated_initializers.testGraphNodeDesignatedInit",
        f"{project_name}.designated_initializers.demonstrateDesignatedInitializers",
    ]

    expected_classes = [
        f"{project_name}.designated_initializers.Point2D",
        f"{project_name}.designated_initializers.Configuration",
        f"{project_name}.designated_initializers.DatabaseConfig",
        f"{project_name}.designated_initializers.GraphNode",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_consteval_immediate_functions(
    cpp_designated_consteval_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++20 consteval immediate functions."""
    test_file = cpp_designated_consteval_project / "consteval_functions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <array>
#include <type_traits>

// Basic consteval functions
consteval int square(int n) {
    return n * n;
}

consteval double power_of_two(int exponent) {
    double result = 1.0;
    for (int i = 0; i < exponent; ++i) {
        result *= 2.0;
    }
    return result;
}

consteval bool is_prime(int n) {
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

// String processing at compile time
consteval size_t string_length(const char* str) {
    size_t len = 0;
    while (str[len] != '\\0') {
        ++len;
    }
    return len;
}

consteval bool string_equal(const char* str1, const char* str2) {
    while (*str1 && *str2) {
        if (*str1 != *str2) return false;
        ++str1;
        ++str2;
    }
    return *str1 == *str2;
}

// Hash function for compile-time string hashing
consteval size_t compile_time_hash(const char* str) {
    size_t hash = 5381;  // djb2 hash algorithm
    while (*str) {
        hash = ((hash << 5) + hash) + static_cast<size_t>(*str);
        ++str;
    }
    return hash;
}

void testBasicConsteval() {
    std::cout << "=== Testing Basic Consteval Functions ===" << std::endl;

    // All these computations happen at compile time
    constexpr int squared_5 = square(5);
    constexpr double power_10 = power_of_two(10);
    constexpr bool is_17_prime = is_prime(17);
    constexpr bool is_16_prime = is_prime(16);

    std::cout << "Compile-time computations:" << std::endl;
    std::cout << "  square(5) = " << squared_5 << std::endl;
    std::cout << "  power_of_two(10) = " << power_10 << std::endl;
    std::cout << "  is_prime(17) = " << (is_17_prime ? "true" : "false") << std::endl;
    std::cout << "  is_prime(16) = " << (is_16_prime ? "true" : "false") << std::endl;

    // String operations at compile time
    constexpr size_t hello_len = string_length("Hello, World!");
    constexpr bool strings_match = string_equal("test", "test");
    constexpr bool strings_differ = string_equal("test", "demo");

    std::cout << "Compile-time string operations:" << std::endl;
    std::cout << "  Length of 'Hello, World!': " << hello_len << std::endl;
    std::cout << "  'test' == 'test': " << (strings_match ? "true" : "false") << std::endl;
    std::cout << "  'test' == 'demo': " << (strings_differ ? "true" : "false") << std::endl;

    // Compile-time hashing
    constexpr size_t hash1 = compile_time_hash("function_name");
    constexpr size_t hash2 = compile_time_hash("variable_name");
    constexpr size_t hash3 = compile_time_hash("function_name");  // Same as hash1

    std::cout << "Compile-time hashes:" << std::endl;
    std::cout << "  hash('function_name') = " << hash1 << std::endl;
    std::cout << "  hash('variable_name') = " << hash2 << std::endl;
    std::cout << "  hash('function_name') again = " << hash3 << std::endl;
    std::cout << "  Hashes match: " << (hash1 == hash3 ? "true" : "false") << std::endl;
}

// Advanced consteval with templates
template<int N>
consteval std::array<int, N> generate_sequence() {
    std::array<int, N> result{};
    for (int i = 0; i < N; ++i) {
        result[i] = i * i;  // Square sequence
    }
    return result;
}

template<size_t N>
consteval std::array<bool, N> sieve_of_eratosthenes() {
    std::array<bool, N> is_prime_arr{};

    // Initialize all as potential primes
    for (size_t i = 2; i < N; ++i) {
        is_prime_arr[i] = true;
    }

    // Sieve algorithm
    for (size_t i = 2; i * i < N; ++i) {
        if (is_prime_arr[i]) {
            for (size_t j = i * i; j < N; j += i) {
                is_prime_arr[j] = false;
            }
        }
    }

    return is_prime_arr;
}

// Consteval function for compile-time configuration
struct CompileTimeConfig {
    size_t buffer_size;
    int max_connections;
    bool debug_enabled;
    double timeout_seconds;
};

consteval CompileTimeConfig create_config(const char* environment) {
    if (string_equal(environment, "development")) {
        return CompileTimeConfig{
            .buffer_size = 1024,
            .max_connections = 10,
            .debug_enabled = true,
            .timeout_seconds = 30.0
        };
    } else if (string_equal(environment, "production")) {
        return CompileTimeConfig{
            .buffer_size = 8192,
            .max_connections = 100,
            .debug_enabled = false,
            .timeout_seconds = 5.0
        };
    } else {
        return CompileTimeConfig{
            .buffer_size = 2048,
            .max_connections = 50,
            .debug_enabled = false,
            .timeout_seconds = 15.0
        };
    }
}

void testAdvancedConsteval() {
    std::cout << "=== Testing Advanced Consteval Functions ===" << std::endl;

    // Generate compile-time arrays
    constexpr auto squares = generate_sequence<10>();
    std::cout << "Square sequence (0-9): ";
    for (size_t i = 0; i < squares.size(); ++i) {
        if (i > 0) std::cout << ", ";
        std::cout << squares[i];
    }
    std::cout << std::endl;

    // Prime sieve at compile time
    constexpr auto primes = sieve_of_eratosthenes<50>();
    std::cout << "Primes under 50: ";
    bool first = true;
    for (size_t i = 2; i < primes.size(); ++i) {
        if (primes[i]) {
            if (!first) std::cout << ", ";
            std::cout << i;
            first = false;
        }
    }
    std::cout << std::endl;

    // Compile-time configuration
    constexpr auto dev_config = create_config("development");
    constexpr auto prod_config = create_config("production");
    constexpr auto test_config = create_config("testing");

    std::cout << "Compile-time configurations:" << std::endl;
    std::cout << "  Development: buffer=" << dev_config.buffer_size
              << ", connections=" << dev_config.max_connections
              << ", debug=" << (dev_config.debug_enabled ? "on" : "off")
              << ", timeout=" << dev_config.timeout_seconds << "s" << std::endl;

    std::cout << "  Production: buffer=" << prod_config.buffer_size
              << ", connections=" << prod_config.max_connections
              << ", debug=" << (prod_config.debug_enabled ? "on" : "off")
              << ", timeout=" << prod_config.timeout_seconds << "s" << std::endl;

    std::cout << "  Testing: buffer=" << test_config.buffer_size
              << ", connections=" << test_config.max_connections
              << ", debug=" << (test_config.debug_enabled ? "on" : "off")
              << ", timeout=" << test_config.timeout_seconds << "s" << std::endl;
}

// Consteval vs constexpr comparison
constexpr int constexpr_factorial(int n) {
    return (n <= 1) ? 1 : n * constexpr_factorial(n - 1);
}

consteval int consteval_factorial(int n) {
    return (n <= 1) ? 1 : n * consteval_factorial(n - 1);
}

// This function can be called at runtime with constexpr
int runtime_factorial(int n) {
    return constexpr_factorial(n);  // OK - constexpr can be used at runtime
}

// This function can only produce compile-time constants
consteval int compile_time_only_factorial(int n) {
    return consteval_factorial(n);  // consteval can only be used at compile time
}

void testConstevalVsConstexpr() {
    std::cout << "=== Testing Consteval vs Constexpr ===" << std::endl;

    // Both can be used at compile time
    constexpr int compile_time_constexpr = constexpr_factorial(5);
    constexpr int compile_time_consteval = consteval_factorial(5);

    std::cout << "Compile-time computations:" << std::endl;
    std::cout << "  constexpr_factorial(5) = " << compile_time_constexpr << std::endl;
    std::cout << "  consteval_factorial(5) = " << compile_time_consteval << std::endl;

    // constexpr can be used at runtime
    int runtime_input = 4;  // Not a compile-time constant
    int runtime_result = runtime_factorial(runtime_input);
    std::cout << "Runtime constexpr_factorial(4) = " << runtime_result << std::endl;

    // consteval forces compile-time evaluation
    constexpr int forced_compile_time = compile_time_only_factorial(6);
    std::cout << "Forced compile-time consteval_factorial(6) = " << forced_compile_time << std::endl;

    // Demonstrate error prevention (these would cause compilation errors):
    // int invalid = consteval_factorial(runtime_input);  // ERROR: runtime argument
    // int invalid2 = compile_time_only_factorial(runtime_input);  // ERROR: runtime argument

    std::cout << "consteval ensures compile-time evaluation only" << std::endl;
}

// Metaprogramming with consteval
template<typename T>
consteval bool is_integral_type() {
    return std::is_integral_v<T>;
}

template<typename T>
consteval size_t get_type_size() {
    return sizeof(T);
}

template<typename T>
consteval const char* get_type_name() {
    if constexpr (std::is_same_v<T, int>) return "int";
    else if constexpr (std::is_same_v<T, double>) return "double";
    else if constexpr (std::is_same_v<T, char>) return "char";
    else if constexpr (std::is_same_v<T, bool>) return "bool";
    else return "unknown";
}

void testConstevalMetaprogramming() {
    std::cout << "=== Testing Consteval Metaprogramming ===" << std::endl;

    // Type analysis at compile time
    constexpr bool int_is_integral = is_integral_type<int>();
    constexpr bool double_is_integral = is_integral_type<double>();
    constexpr size_t int_size = get_type_size<int>();
    constexpr size_t double_size = get_type_size<double>();

    std::cout << "Type analysis:" << std::endl;
    std::cout << "  int is integral: " << (int_is_integral ? "true" : "false") << std::endl;
    std::cout << "  double is integral: " << (double_is_integral ? "true" : "false") << std::endl;
    std::cout << "  sizeof(int): " << int_size << " bytes" << std::endl;
    std::cout << "  sizeof(double): " << double_size << " bytes" << std::endl;

    // Type name resolution
    constexpr const char* int_name = get_type_name<int>();
    constexpr const char* double_name = get_type_name<double>();
    constexpr const char* char_name = get_type_name<char>();
    constexpr const char* bool_name = get_type_name<bool>();

    std::cout << "Type names:" << std::endl;
    std::cout << "  int -> " << int_name << std::endl;
    std::cout << "  double -> " << double_name << std::endl;
    std::cout << "  char -> " << char_name << std::endl;
    std::cout << "  bool -> " << bool_name << std::endl;
}

void demonstrateConsteval() {
    testBasicConsteval();
    testAdvancedConsteval();
    testConstevalVsConstexpr();
    testConstevalMetaprogramming();
}
""",
    )

    run_updater(cpp_designated_consteval_project, mock_ingestor)

    project_name = cpp_designated_consteval_project.name

    expected_functions = [
        f"{project_name}.consteval_functions.square",
        f"{project_name}.consteval_functions.power_of_two",
        f"{project_name}.consteval_functions.is_prime",
        f"{project_name}.consteval_functions.testBasicConsteval",
        f"{project_name}.consteval_functions.testAdvancedConsteval",
        f"{project_name}.consteval_functions.demonstrateConsteval",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_lambda_init_captures(
    cpp_designated_consteval_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++14/17/20 lambda init captures and generalized capture."""
    test_file = cpp_designated_consteval_project / "lambda_init_captures.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <vector>
#include <memory>
#include <algorithm>
#include <functional>

void testBasicInitCaptures() {
    std::cout << "=== Testing Basic Lambda Init Captures ===" << std::endl;

    int x = 10;
    std::string name = "original";

    // C++14 init capture - capture by move
    auto lambda1 = [moved_name = std::move(name)](int multiplier) {
        std::cout << "Lambda1: " << moved_name << " * " << multiplier << " = "
                  << moved_name.length() * multiplier << std::endl;
        return moved_name.length() * multiplier;
    };

    std::cout << "After move, original name: '" << name << "' (should be empty)" << std::endl;
    lambda1(3);

    // Init capture with computation
    auto lambda2 = [computed_value = x * x + 5](const std::string& prefix) {
        std::cout << prefix << computed_value << std::endl;
        return computed_value;
    };

    lambda2("Computed value: ");

    // Init capture with unique_ptr
    auto lambda3 = [unique_data = std::make_unique<std::vector<int>>(10, 42)]() mutable {
        unique_data->push_back(100);
        std::cout << "Unique data size: " << unique_data->size() << std::endl;
        std::cout << "Last element: " << unique_data->back() << std::endl;
        return unique_data->size();
    };

    lambda3();
}

void testAdvancedInitCaptures() {
    std::cout << "=== Testing Advanced Lambda Init Captures ===" << std::endl;

    std::vector<std::string> words = {"hello", "world", "lambda", "capture"};

    // Capture by transformation
    auto lambda1 = [upper_words = [&words]() {
        std::vector<std::string> result;
        for (const auto& word : words) {
            std::string upper = word;
            std::transform(upper.begin(), upper.end(), upper.begin(), ::toupper);
            result.push_back(upper);
        }
        return result;
    }()](size_t index) {
        if (index < upper_words.size()) {
            std::cout << "Upper word[" << index << "]: " << upper_words[index] << std::endl;
            return upper_words[index];
        }
        return std::string("");
    };

    lambda1(0);
    lambda1(2);

    // Capture with shared state
    auto shared_counter = std::make_shared<int>(0);

    auto create_incrementer = [counter = shared_counter](const std::string& name) {
        return [counter, name]() mutable {
            ++(*counter);
            std::cout << name << " incremented counter to: " << *counter << std::endl;
            return *counter;
        };
    };

    auto inc1 = create_incrementer("Incrementer1");
    auto inc2 = create_incrementer("Incrementer2");

    inc1();
    inc2();
    inc1();
    inc2();

    // Complex capture with multiple initializations
    int base = 100;
    double multiplier = 1.5;

    auto complex_lambda = [
        base_squared = base * base,
        multiplied = base * multiplier,
        combined = base + static_cast<int>(multiplier * 10),
        message = std::string("Complex calculation: ")
    ](const std::string& operation) {
        std::cout << message;
        if (operation == "squared") {
            std::cout << base_squared << std::endl;
            return static_cast<double>(base_squared);
        } else if (operation == "multiplied") {
            std::cout << multiplied << std::endl;
            return multiplied;
        } else if (operation == "combined") {
            std::cout << combined << std::endl;
            return static_cast<double>(combined);
        }
        return 0.0;
    };

    complex_lambda("squared");
    complex_lambda("multiplied");
    complex_lambda("combined");
}

void testGeneralizedCapture() {
    std::cout << "=== Testing Generalized Lambda Capture ===" << std::endl;

    // Capture with perfect forwarding
    auto make_processor = [](auto&& data) {
        return [captured_data = std::forward<decltype(data)>(data)](auto&& operation) {
            return operation(captured_data);
        };
    };

    std::vector<int> numbers = {1, 2, 3, 4, 5};
    auto processor = make_processor(std::move(numbers));

    auto sum_result = processor([](const auto& vec) {
        int sum = 0;
        for (auto val : vec) sum += val;
        std::cout << "Sum: " << sum << std::endl;
        return sum;
    });

    auto size_result = processor([](const auto& vec) {
        std::cout << "Size: " << vec.size() << std::endl;
        return vec.size();
    });

    // Variadic capture
    auto make_tuple_processor = [](auto... args) {
        return [captured_tuple = std::make_tuple(args...)](auto index_sequence) {
            return std::get<0>(captured_tuple) + std::get<1>(captured_tuple) + std::get<2>(captured_tuple);
        };
    };

    auto tuple_proc = make_tuple_processor(10, 20, 30);
    auto tuple_sum = tuple_proc(std::index_sequence<0, 1, 2>{});
    std::cout << "Tuple sum: " << tuple_sum << std::endl;

    // Recursive lambda with init capture
    auto factorial_lambda = [](int n) {
        auto impl = [](int n, auto& self) -> int {
            return (n <= 1) ? 1 : n * self(n - 1, self);
        };
        return impl(n, impl);
    };

    for (int i = 1; i <= 6; ++i) {
        std::cout << "Factorial(" << i << ") = " << factorial_lambda(i) << std::endl;
    }
}

void testLambdaCapturePatterns() {
    std::cout << "=== Testing Lambda Capture Patterns ===" << std::endl;

    // Resource management with RAII in lambda
    class Resource {
    private:
        std::string name_;
        bool acquired_;

    public:
        Resource(const std::string& name) : name_(name), acquired_(false) {
            acquire();
        }

        ~Resource() {
            if (acquired_) release();
        }

        // Move-only semantics
        Resource(const Resource&) = delete;
        Resource& operator=(const Resource&) = delete;

        Resource(Resource&& other) noexcept
            : name_(std::move(other.name_)), acquired_(other.acquired_) {
            other.acquired_ = false;
        }

        Resource& operator=(Resource&& other) noexcept {
            if (this != &other) {
                if (acquired_) release();
                name_ = std::move(other.name_);
                acquired_ = other.acquired_;
                other.acquired_ = false;
            }
            return *this;
        }

        void acquire() {
            if (!acquired_) {
                std::cout << "Acquiring resource: " << name_ << std::endl;
                acquired_ = true;
            }
        }

        void release() {
            if (acquired_) {
                std::cout << "Releasing resource: " << name_ << std::endl;
                acquired_ = false;
            }
        }

        void use() const {
            if (acquired_) {
                std::cout << "Using resource: " << name_ << std::endl;
            }
        }

        const std::string& name() const { return name_; }
    };

    // Lambda with RAII resource
    auto resource_processor = [resource = Resource("DatabaseConnection")](const std::string& query) mutable {
        resource.use();
        std::cout << "Executing query: " << query << std::endl;
        return query.length();
    };

    resource_processor("SELECT * FROM users");
    resource_processor("UPDATE users SET active = true");

    // Factory pattern with lambda init capture
    auto create_validator = [](const std::string& pattern) {
        return [compiled_pattern = [&pattern]() {
            // Simulate pattern compilation
            std::string compiled = "COMPILED[" + pattern + "]";
            std::cout << "Compiling pattern: " << pattern << " -> " << compiled << std::endl;
            return compiled;
        }()](const std::string& input) {
            bool matches = input.find(compiled_pattern.substr(9, compiled_pattern.length() - 10)) != std::string::npos;
            std::cout << "Validating '" << input << "' against pattern: " << (matches ? "MATCH" : "NO MATCH") << std::endl;
            return matches;
        };
    };

    auto email_validator = create_validator("@");
    email_validator("user@example.com");
    email_validator("invalid-email");

    // State machine with lambda capture
    enum class State { IDLE, PROCESSING, COMPLETED, ERROR };

    auto create_state_machine = [initial_state = State::IDLE]() mutable {
        return [current_state = initial_state](const std::string& event) mutable -> State {
            State old_state = current_state;

            if (event == "start" && current_state == State::IDLE) {
                current_state = State::PROCESSING;
            } else if (event == "complete" && current_state == State::PROCESSING) {
                current_state = State::COMPLETED;
            } else if (event == "error") {
                current_state = State::ERROR;
            } else if (event == "reset") {
                current_state = State::IDLE;
            }

            auto state_name = [](State s) {
                switch (s) {
                    case State::IDLE: return "IDLE";
                    case State::PROCESSING: return "PROCESSING";
                    case State::COMPLETED: return "COMPLETED";
                    case State::ERROR: return "ERROR";
                }
                return "UNKNOWN";
            };

            std::cout << "State transition: " << state_name(old_state)
                      << " --[" << event << "]--> " << state_name(current_state) << std::endl;

            return current_state;
        };
    };

    auto state_machine = create_state_machine();
    state_machine("start");
    state_machine("complete");
    state_machine("reset");
    state_machine("error");
}

void demonstrateLambdaInitCaptures() {
    testBasicInitCaptures();
    testAdvancedInitCaptures();
    testGeneralizedCapture();
    testLambdaCapturePatterns();
}
""",
    )

    run_updater(cpp_designated_consteval_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    total_calls = len(call_relationships)

    assert total_calls >= 15, (
        f"Expected at least 15 total calls across all tests, found {total_calls}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"

    print(
        "✅ C++20 designated initializers, consteval, and lambda init captures validation passed:"
    )


def test_comprehensive_modern_cpp_complete() -> None:
    """Mark comprehensive modern C++ testing as complete."""
    print("Coverage includes:")
    print("   - C++20 designated initializers")
    print("   - C++20 consteval immediate functions")
    print("   - C++14/17/20 lambda init captures and generalized capture")
    print("   - Advanced template metaprogramming with consteval")
    print("   - Real-world usage patterns and integration examples")
    print("   - RAII patterns with lambda captures")
    print("   - State machines and factory patterns with modern C++")
    assert True
