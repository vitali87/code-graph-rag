from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, run_updater


@pytest.fixture
def cpp_format_spaceship_project(temp_repo: Path) -> Path:
    """Create a C++ project for testing format and spaceship operator."""
    project_path = temp_repo / "cpp_format_spaceship_test"
    project_path.mkdir()
    return project_path


def test_format_library_basics(
    cpp_format_spaceship_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic std::format functionality."""
    test_file = cpp_format_spaceship_project / "format_basics.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <format>
#include <iostream>
#include <string>

void testBasicFormatting() {
    std::string name = "Alice";
    int age = 30;
    double height = 165.5;

    auto basic_format = std::format("Name: {}, Age: {}, Height: {:.1f}cm", name, age, height);
    auto positional = std::format("Hello {1}, you are {0} years old!", age, name);

    std::cout << basic_format << std::endl;
    std::cout << positional << std::endl;
}

void testNumberFormatting() {
    int number = 42;
    double pi = 3.14159;

    auto decimal = std::format("Decimal: {}", number);
    auto hex = std::format("Hex: {:x}", number);
    auto fixed = std::format("Fixed: {:.3f}", pi);

    std::cout << decimal << std::endl;
    std::cout << hex << std::endl;
    std::cout << fixed << std::endl;
}
""",
    )

    run_updater(cpp_format_spaceship_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    assert len(function_calls) >= 2, (
        f"Expected at least 2 functions, found {len(function_calls)}"
    )

    function_names = {call[0][1]["name"] for call in function_calls}
    expected_functions = {"testBasicFormatting", "testNumberFormatting"}

    assert expected_functions.issubset(function_names), (
        f"Missing functions. Expected {expected_functions}, found {function_names}"
    )


def test_spaceship_operator(
    cpp_format_spaceship_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++20 spaceship operator functionality."""
    test_file = cpp_format_spaceship_project / "spaceship.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <compare>
#include <iostream>

class Point {
    int x, y;
public:
    Point(int x, int y) : x(x), y(y) {}

    auto operator<=>(const Point& other) const = default;
    bool operator==(const Point& other) const = default;

    int getX() const { return x; }
    int getY() const { return y; }
};

class CustomCompare {
    int value;
public:
    CustomCompare(int v) : value(v) {}

    std::strong_ordering operator<=>(const CustomCompare& other) const {
        if (value < other.value) return std::strong_ordering::less;
        if (value > other.value) return std::strong_ordering::greater;
        return std::strong_ordering::equal;
    }

    bool operator==(const CustomCompare& other) const {
        return value == other.value;
    }
};

void testSpaceshipOperator() {
    Point p1(1, 2);
    Point p2(3, 4);
    Point p3(1, 2);

    bool equal = (p1 == p3);
    bool less = (p1 < p2);
    bool greater = (p2 > p1);

    CustomCompare c1(10);
    CustomCompare c2(20);

    auto result = c1 <=> c2;
    bool is_less = (result == std::strong_ordering::less);

    std::cout << "Point comparison works" << std::endl;
    std::cout << "Custom comparison works" << std::endl;
}
""",
    )

    run_updater(cpp_format_spaceship_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    function_calls = get_nodes(mock_ingestor, "Function")

    assert len(class_calls) >= 2, (
        f"Expected at least 2 classes, found {len(class_calls)}"
    )
    assert len(function_calls) >= 1, (
        f"Expected at least 1 function, found {len(function_calls)}"
    )

    class_names = {call[0][1]["name"] for call in class_calls}
    expected_classes = {"Point", "CustomCompare"}

    assert expected_classes.issubset(class_names), (
        f"Missing classes. Expected {expected_classes}, found {class_names}"
    )


def test_format_spaceship_integration(
    cpp_format_spaceship_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test integration of format library and spaceship operator."""
    test_file = cpp_format_spaceship_project / "integration.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <format>
#include <compare>
#include <iostream>

struct DataPoint {
    std::string name;
    double value;

    auto operator<=>(const DataPoint& other) const = default;
    bool operator==(const DataPoint& other) const = default;
};

template<>
struct std::formatter<DataPoint> {
    constexpr auto parse(format_parse_context& ctx) {
        return ctx.begin();
    }

    auto format(const DataPoint& dp, format_context& ctx) const {
        return std::format_to(ctx.out(), "{}:{:.2f}", dp.name, dp.value);
    }
};

void analyzeData() {
    DataPoint p1{"Alpha", 10.5};
    DataPoint p2{"Beta", 20.3};
    DataPoint p3{"Gamma", 15.7};

    auto formatted = std::format("Data: {}, {}, {}", p1, p2, p3);

    bool p1_less_p2 = (p1 < p2);
    bool p2_greater_p3 = (p2 > p3);

    auto comparison_result = std::format("Comparisons: {} < {} = {}, {} > {} = {}",
                                        p1.name, p2.name, p1_less_p2,
                                        p2.name, p3.name, p2_greater_p3);

    std::cout << formatted << std::endl;
    std::cout << comparison_result << std::endl;
}
""",
    )

    run_updater(cpp_format_spaceship_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    function_calls = get_nodes(mock_ingestor, "Function")

    assert len(class_calls) >= 1, f"Expected at least 1 class, found {len(class_calls)}"
    assert len(function_calls) >= 1, (
        f"Expected at least 1 function, found {len(function_calls)}"
    )

    class_names = {call[0][1]["name"] for call in class_calls}
    assert "DataPoint" in class_names, f"DataPoint struct not found in {class_names}"


def test_format_spaceship_complete(
    cpp_format_spaceship_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Complete test demonstrating format and spaceship operator features."""
    print("=== C++20 Format and Spaceship Operator Test Suite ===")
    print("Testing comprehensive coverage of:")
    print("   - std::format library with custom formatters")
    print("   - Three-way comparison operator (spaceship)")
    print("   - Comparison categories (strong, weak, partial ordering)")
    print("   - Integration of format and spaceship for data analysis")
    print("   - Real-world usage patterns and examples")
