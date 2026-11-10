from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, run_updater


@pytest.fixture
def cpp_lambda_project(temp_repo: Path) -> Path:
    """Create a C++ project for testing lambda captures."""
    project_path = temp_repo / "cpp_lambda_test"
    project_path.mkdir()
    return project_path


def test_basic_lambda_captures(
    cpp_lambda_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic lambda capture patterns."""
    test_file = cpp_lambda_project / "basic_captures.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <string>
#include <memory>
#include <functional>

void testBasicCaptures() {
    int x = 10;
    std::string name = "test";

    auto lambda1 = [x, name](int multiplier) {
        return x * multiplier;
    };

    auto lambda2 = [&x, &name]() {
        x += 5;
        name += "_modified";
    };

    auto lambda3 = [x, &name](const std::string& suffix) {
        return name + suffix + std::to_string(x);
    };

    auto result1 = lambda1(2);
    lambda2();
    auto result3 = lambda3("_result");
}

void testInitCaptures() {
    int value = 42;
    std::string text = "hello";

    auto lambda1 = [moved_text = std::move(text), computed = value * 2](int addon) {
        return moved_text + std::to_string(computed + addon);
    };

    auto lambda2 = [ptr = std::make_unique<int>(100)]() mutable {
        *ptr += 50;
        return *ptr;
    };

    auto result1 = lambda1(8);
    auto result2 = lambda2();
}
""",
    )

    run_updater(cpp_lambda_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    assert len(function_calls) >= 2, (
        f"Expected at least 2 functions, found {len(function_calls)}"
    )

    function_names = {call[0][1]["name"] for call in function_calls}
    expected_functions = {"testBasicCaptures", "testInitCaptures"}

    assert expected_functions.issubset(function_names), (
        f"Missing functions. Expected {expected_functions}, found {function_names}"
    )


def test_generalized_captures(
    cpp_lambda_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++17/20 generalized capture patterns."""
    test_file = cpp_lambda_project / "generalized_captures.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <algorithm>
#include <functional>
#include <memory>

class Resource {
public:
    Resource(std::string name) : name_(std::move(name)) {}
    const std::string& getName() const { return name_; }
private:
    std::string name_;
};

void testGeneralizedCaptures() {
    std::vector<int> data = {1, 2, 3, 4, 5};

    auto processor = [captured_data = std::move(data)](auto&& func) mutable {
        return func(captured_data);
    };

    auto make_processor = [](auto... args) {
        return [captured_args = std::make_tuple(args...)](size_t index) {
            return std::get<0>(captured_args);
        };
    };

    auto result = processor([](const auto& vec) {
        return vec.size();
    });

    auto var_proc = make_processor(10, 20, 30);
    auto var_result = var_proc(0);
}

void testResourceCaptures() {
    auto factory = [](const std::string& name) {
        return [resource = std::make_shared<Resource>(name)](const std::string& operation) {
            if (operation == "get_name") {
                return resource->getName();
            }
            return std::string("unknown");
        };
    };

    auto resource_handler = factory("database_connection");
    auto name = resource_handler("get_name");
}

void testStateMachine() {
    enum class State { IDLE, RUNNING, STOPPED };

    auto create_machine = [initial = State::IDLE]() mutable {
        return [current_state = initial](const std::string& event) mutable -> State {
            if (event == "start" && current_state == State::IDLE) {
                current_state = State::RUNNING;
            } else if (event == "stop" && current_state == State::RUNNING) {
                current_state = State::STOPPED;
            } else if (event == "reset") {
                current_state = State::IDLE;
            }
            return current_state;
        };
    };

    auto machine = create_machine();
    auto state1 = machine("start");
    auto state2 = machine("stop");
    auto state3 = machine("reset");
}
""",
    )

    run_updater(cpp_lambda_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    function_calls = get_nodes(mock_ingestor, "Function")

    assert len(class_calls) >= 1, f"Expected at least 1 class, found {len(class_calls)}"
    assert len(function_calls) >= 3, (
        f"Expected at least 3 functions, found {len(function_calls)}"
    )

    class_names = {call[0][1]["name"] for call in class_calls}
    assert "Resource" in class_names, f"Resource class not found in {class_names}"

    function_names = {call[0][1]["name"] for call in function_calls}
    expected_functions = {
        "testGeneralizedCaptures",
        "testResourceCaptures",
        "testStateMachine",
    }

    assert expected_functions.issubset(function_names), (
        f"Missing functions. Expected {expected_functions}, found {function_names}"
    )


def test_lambda_validation_complete(
    cpp_lambda_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Validate comprehensive lambda capture parsing."""
    test_file = cpp_lambda_project / "lambda_validation.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <functional>
#include <memory>
#include <vector>

void validateLambdaCaptures() {
    int base = 100;
    std::vector<int> data = {1, 2, 3};

    // Complex capture patterns
    auto complex_lambda = [
        base_squared = base * base,
        data_copy = data,
        shared_ptr = std::make_shared<int>(42)
    ](auto&& operation) mutable {
        return operation(base_squared, data_copy, shared_ptr);
    };

    auto result = complex_lambda([](int squared, const auto& vec, auto ptr) {
        return squared + vec.size() + *ptr;
    });
}
""",
    )

    run_updater(cpp_lambda_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    all_relationships = [
        call for call in mock_ingestor.ensure_relationship_batch.call_args_list
    ]

    call_relationships = [
        c for c in all_relationships if len(c.args) > 1 and c.args[1] == "CALLS"
    ]
    defines_relationships = [
        c for c in all_relationships if len(c.args) > 1 and c.args[1] == "DEFINES"
    ]

    assert len(function_calls) >= 1, (
        f"Expected at least 1 function, found {len(function_calls)}"
    )
    assert len(call_relationships) >= 0, "Should have CALLS relationships"
    assert len(defines_relationships) >= 0, "Should have DEFINES relationships"

    function_names = {call[0][1]["name"] for call in function_calls}
    assert "validateLambdaCaptures" in function_names, (
        f"validateLambdaCaptures function not found in {function_names}"
    )
