from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def cpp_concepts_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with concepts patterns."""
    project_path = temp_repo / "cpp_concepts_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    (project_path / "include" / "concepts.h").write_text(
        encoding="utf-8", data="#pragma once\n// Concepts header"
    )

    return project_path


def test_concept_definitions_and_constraints(
    cpp_concepts_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic concept definitions and template constraints."""
    test_file = cpp_concepts_project / "concept_definitions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <concepts>
#include <type_traits>
#include <vector>
#include <string>
#include <iterator>

// Basic concept definition
template<typename T>
concept Integral = std::is_integral_v<T>;

// Compound concept with multiple requirements
template<typename T>
concept SignedIntegral = Integral<T> && std::is_signed_v<T>;

// Concept with requires clause
template<typename T>
concept Addable = requires(T a, T b) {
    a + b;
    { a + b } -> std::same_as<T>;
};

// Complex concept with nested requirements
template<typename T>
concept Container = requires(T t) {
    typename T::value_type;
    typename T::iterator;
    { t.begin() } -> std::same_as<typename T::iterator>;
    { t.end() } -> std::same_as<typename T::iterator>;
    { t.size() } -> std::convertible_to<std::size_t>;
};

// Function template with concept constraint
template<Integral T>
T multiply(T a, T b) {
    return a * b;
}

// Function with requires clause
template<typename T>
requires Addable<T>
T add_values(T a, T b) {
    return a + b;
}

// Class template with concept constraint
template<Container C>
class ContainerProcessor {
public:
    using ValueType = typename C::value_type;

    explicit ContainerProcessor(const C& container)
        : container_(container) {}

    std::size_t process() {
        return container_.size();
    }

private:
    const C& container_;
};

// Concept with auto parameter
auto process_integral(Integral auto value) {
    return value * 2;
}

// Abbreviated function template
void print_addable(Addable auto value) {
    // Process addable type
}

void demonstrateBasicConcepts() {
    // Test integral concepts
    int x = 42;
    long y = 100L;

    auto result1 = multiply(x, 5);
    auto result2 = multiply(y, 3L);

    // Test container concepts
    std::vector<int> vec{1, 2, 3, 4, 5};
    ContainerProcessor processor(vec);
    auto size = processor.process();

    // Test abbreviated templates
    auto doubled = process_integral(42);
    print_addable(3.14);
}
""",
    )

    run_updater(cpp_concepts_project, mock_ingestor)

    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    concept_definitions = [
        call
        for call in defines_relationships
        if "concept_definitions" in call.args[0][2]
        and (
            "concept" in call.args[2][2].lower()
            or "template" in call.args[2][2].lower()
        )
    ]

    assert len(concept_definitions) >= 3, (
        f"Expected at least 3 concept-related definitions, found {len(concept_definitions)}"
    )


def test_advanced_concept_patterns(
    cpp_concepts_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced concepts usage patterns and custom concept definitions."""
    test_file = cpp_concepts_project / "advanced_concepts.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <concepts>
#include <type_traits>
#include <memory>
#include <iostream>

namespace custom_concepts {

// Advanced concept for smart pointer types
template<typename T>
concept SmartPointer = requires(T t) {
    typename T::element_type;
    { t.get() } -> std::convertible_to<typename T::element_type*>;
    { t.reset() } -> std::same_as<void>;
    { static_cast<bool>(t) } -> std::same_as<bool>;
};

// Concept for movable types
template<typename T>
concept Movable = std::is_move_constructible_v<T> &&
                  std::is_move_assignable_v<T>;

// Resource management concept
template<typename T>
concept Resource = requires(T t) {
    { t.acquire() } -> std::same_as<void>;
    { t.release() } -> std::same_as<void>;
    { t.is_valid() } -> std::convertible_to<bool>;
};

// Serializable concept
template<typename T>
concept Serializable = requires(T t, std::ostream& os, std::istream& is) {
    { t.serialize(os) } -> std::same_as<void>;
    { T::deserialize(is) } -> std::same_as<T>;
};

// Polymorphic concept
template<typename T, typename Base>
concept DerivedFrom = std::is_base_of_v<Base, T> &&
                      std::is_convertible_v<T*, Base*>;

}  // namespace custom_concepts

using namespace custom_concepts;

// Smart pointer implementation
template<SmartPointer T>
class SmartPointerWrapper {
private:
    T ptr_;

public:
    explicit SmartPointerWrapper(T ptr) : ptr_(std::move(ptr)) {}

    auto get() const { return ptr_.get(); }
    void reset() { ptr_.reset(); }
    operator bool() const { return static_cast<bool>(ptr_); }
};

// Resource management with RAII
template<Resource R>
class ResourceManager {
private:
    R resource_;
    bool owned_;

public:
    explicit ResourceManager(R resource)
        : resource_(std::move(resource)), owned_(true) {
        resource_.acquire();
    }

    ~ResourceManager() {
        if (owned_ && resource_.is_valid()) {
            resource_.release();
        }
    }

    ResourceManager(const ResourceManager&) = delete;
    ResourceManager& operator=(const ResourceManager&) = delete;

    ResourceManager(ResourceManager&& other) noexcept
        : resource_(std::move(other.resource_)), owned_(other.owned_) {
        other.owned_ = false;
    }

    const R& get() const { return resource_; }
};

// Factory pattern with concepts
template<typename T>
requires std::is_default_constructible_v<T>
class SimpleFactory {
public:
    T create() {
        return T{};
    }

    void destroy(T& object) {
        // Custom destruction logic if needed
    }
};

void demonstrateAdvancedConcepts() {
    // Smart pointer concept
    auto unique_ptr = std::make_unique<int>(42);
    SmartPointerWrapper wrapper(std::move(unique_ptr));

    // Factory concept
    SimpleFactory<std::string> factory;
    auto str = factory.create();
    factory.destroy(str);
}
""",
    )

    run_updater(cpp_concepts_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    advanced_calls = [
        call for call in call_relationships if "advanced_concepts" in call.args[0][2]
    ]

    assert len(advanced_calls) >= 5, (
        f"Expected at least 5 advanced concept calls, found {len(advanced_calls)}"
    )


def test_concept_composition_and_specialization(
    cpp_concepts_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test concept composition, specialization, and nested concepts."""
    test_file = cpp_concepts_project / "concept_composition.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <concepts>
#include <type_traits>
#include <iostream>

// Basic concepts
template<typename T>
concept Arithmetic = std::is_arithmetic_v<T>;

// Concept specialization
template<typename T>
concept Number = Arithmetic<T> && !std::is_same_v<T, bool>;

template<Number T>
class Calculator {
public:
    T calculate(T a, T b) {
        return a + b;
    }
};

// Nested concepts
namespace math {
    template<typename T>
    concept Numeric = std::is_arithmetic_v<T>;

    template<Numeric T>
    concept Positive = requires(T t) {
        requires t > T{0};
    };

    template<Positive T>
    T square_root_estimate(T value) {
        return value / 2;  // Simplified
    }
}

// Concept composition
template<typename T>
concept Container = requires(T t) {
    typename T::value_type;
    { t.begin() };
    { t.end() };
    { t.size() } -> std::convertible_to<std::size_t>;
};

template<typename T>
concept IterableContainer = Container<T> && requires(T t) {
    std::begin(t);
    std::end(t);
};

template<IterableContainer T>
void process_elements(const T& container) {
    for (const auto& element : container) {
        // Process element
    }
}

void demonstrateConceptComposition() {
    // Test numeric concepts
    Calculator<double> calc;
    auto sum = calc.calculate(1.5, 2.7);

    // Test nested concepts
    auto estimate = math::square_root_estimate(16.0);

    // Test container concepts
    std::vector<int> vec{1, 2, 3, 4, 5};
    process_elements(vec);
}
""",
    )

    run_updater(cpp_concepts_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    namespace_relationships = [
        call
        for call in all_relationships
        if "concept_composition" in call.args[0][2] and "math" in str(call.args).lower()
    ]

    assert len(namespace_relationships) >= 1, (
        f"Expected namespace relationships for concepts, found {len(namespace_relationships)}"
    )

    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    composition_definitions = [
        call
        for call in defines_relationships
        if "concept_composition" in call.args[0][2]
    ]

    assert len(composition_definitions) >= 5, (
        f"Expected at least 5 concept composition definitions, found {len(composition_definitions)}"
    )
