from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def cpp_includes_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with all include patterns."""
    project_path = temp_repo / "cpp_includes_test"
    project_path.mkdir()

    (project_path / "include").mkdir()
    (project_path / "include" / "common").mkdir()
    (project_path / "include" / "utils").mkdir()
    (project_path / "src").mkdir()
    (project_path / "external").mkdir()

    (project_path / "include" / "base.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
class Base {
public:
    virtual ~Base() = default;
    virtual void process() = 0;
};
""",
    )

    (project_path / "include" / "common" / "types.h").write_text(
        encoding="utf-8",
        data="""
#ifndef TYPES_H
#define TYPES_H

typedef int ID;
typedef double Real;

enum Status {
    SUCCESS,
    FAILURE,
    PENDING
};

#endif // TYPES_H
""",
    )

    (project_path / "include" / "utils" / "math.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
namespace utils {
    namespace math {
        double add(double a, double b);
        double multiply(double a, double b);
        const double PI = 3.14159;
    }
}
""",
    )

    (project_path / "external" / "third_party.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
namespace external {
    void thirdPartyFunction();
}
""",
    )

    return project_path


def test_standard_library_includes(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test standard library include parsing and relationship creation."""
    test_file = cpp_includes_project / "stdlib_includes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Standard C++ library includes
#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <algorithm>
#include <memory>
#include <thread>
#include <mutex>
#include <fstream>
#include <sstream>

// C standard library includes
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cassert>

// Using standard library features
void demonstrateStdLibrary() {
    // iostream
    std::cout << "Hello, World!" << std::endl;

    // string and vector
    std::string message = "C++ Standard Library";
    std::vector<int> numbers = {1, 2, 3, 4, 5};

    // map
    std::map<std::string, int> ages;
    ages["Alice"] = 30;
    ages["Bob"] = 25;

    // algorithm
    std::sort(numbers.begin(), numbers.end());
    auto it = std::find(numbers.begin(), numbers.end(), 3);

    // memory
    std::unique_ptr<int> ptr = std::make_unique<int>(42);
    std::shared_ptr<std::string> shared = std::make_shared<std::string>("shared");

    // thread and mutex
    std::mutex mtx;
    std::thread t([&mtx]() {
        std::lock_guard<std::mutex> lock(mtx);
        // thread work
    });
    t.join();

    // file streams
    std::ifstream input("data.txt");
    std::ofstream output("result.txt");
    std::stringstream ss;
    ss << "stringstream content";

    // C standard library
    printf("C-style output\\n");
    double result = sqrt(16.0);
    assert(result == 4.0);
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    stdlib_imports = [
        call for call in import_relationships if "stdlib_includes" in call.args[0][2]
    ]

    assert len(stdlib_imports) >= 10, (
        f"Expected at least 10 stdlib includes, found {len(stdlib_imports)}"
    )

    imported_headers = [call.args[2][2] for call in stdlib_imports]
    expected_headers = [
        "iostream",
        "string",
        "vector",
        "map",
        "algorithm",
        "memory",
        "thread",
        "fstream",
        "cstdio",
        "cmath",
    ]

    for expected in expected_headers:
        assert any(expected in header for header in imported_headers), (
            f"Missing stdlib include: {expected}\nFound: {imported_headers}"
        )


def test_local_header_includes(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test local header include parsing and relationship creation."""
    test_file = cpp_includes_project / "src" / "local_includes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Local header includes with different path styles
#include "base.h"
#include "../include/base.h"
#include "../include/common/types.h"
#include "../include/utils/math.h"

// Relative path includes
#include "../external/third_party.h"

// Include with implementation
#include "local_includes.h"

class Derived : public Base {
public:
    void process() override {
        // Using types from types.h
        ID identifier = 100;
        Real value = 3.14;
        Status status = SUCCESS;

        // Using functions from math.h
        double sum = utils::math::add(10.0, 20.0);
        double product = utils::math::multiply(5.0, utils::math::PI);

        // Using external library
        external::thirdPartyFunction();
    }

    void calculateResults() {
        Real a = 1.5, b = 2.5;
        Real result = utils::math::add(a, b);

        Status currentStatus = PENDING;
        if (result > 0) {
            currentStatus = SUCCESS;
        }
    }
};

void useLocalHeaders() {
    Derived derived;
    derived.process();
    derived.calculateResults();

    // Direct usage of included types
    std::vector<ID> identifiers = {1, 2, 3, 4, 5};
    std::map<ID, Real> values;

    for (ID id : identifiers) {
        values[id] = utils::math::multiply(id, utils::math::PI);
    }
}
""",
    )

    header_file = cpp_includes_project / "src" / "local_includes.h"
    header_file.write_text(
        encoding="utf-8",
        data="""
#pragma once
#include "../include/base.h"

class Derived : public Base {
public:
    void process() override;
    void calculateResults();
};

void useLocalHeaders();
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    local_imports = [
        call for call in import_relationships if "local_includes" in call.args[0][2]
    ]

    assert len(local_imports) >= 4, (
        f"Expected at least 4 local includes, found {len(local_imports)}"
    )

    imported_headers = [call.args[2][2] for call in local_imports]
    expected_patterns = [
        "base",
        "types",
        "math",
        "third_party",
    ]

    for pattern in expected_patterns:
        assert any(pattern in header for header in imported_headers), (
            f"Missing local include pattern: {pattern}\nFound: {imported_headers}"
        )


def test_conditional_includes(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test conditional include patterns with preprocessor directives."""
    test_file = cpp_includes_project / "conditional_includes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Platform-specific includes
#ifdef _WIN32
#include <windows.h>
#include <direct.h>
#elif defined(__linux__)
#include <unistd.h>
#include <sys/types.h>
#include <dirent.h>
#elif defined(__APPLE__)
#include <sys/types.h>
#include <sys/stat.h>
#include <dirent.h>
#endif

// Compiler-specific includes
#if defined(__GNUC__)
#include <ext/algorithm>
#elif defined(_MSC_VER)
#include <intrin.h>
#endif

// Feature availability includes
#if __cplusplus >= 201703L
#include <optional>
#include <variant>
#include <filesystem>
#endif

#if __cplusplus >= 202002L
#include <concepts>
#include <ranges>
#endif

// Debug vs Release includes
#ifdef DEBUG
#include <cassert>
#include <iostream>
#define DBG_PRINT(x) std::cout << x << std::endl
#else
#define DBG_PRINT(x)
#endif

// Standard includes that are always present
#include <string>
#include <vector>
#include <memory>

// Conditional compilation example
class PlatformUtils {
public:
    static std::string getCurrentDirectory() {
#ifdef _WIN32
        char buffer[MAX_PATH];
        _getcwd(buffer, MAX_PATH);
        return std::string(buffer);
#else
        char buffer[1024];
        getcwd(buffer, sizeof(buffer));
        return std::string(buffer);
#endif
    }

    static std::vector<std::string> listDirectory(const std::string& path) {
        std::vector<std::string> files;

#ifdef _WIN32
        // Windows implementation would go here
        DBG_PRINT("Using Windows directory listing");
#else
        DIR* dir = opendir(path.c_str());
        if (dir) {
            struct dirent* entry;
            while ((entry = readdir(dir)) != nullptr) {
                files.push_back(entry->d_name);
            }
            closedir(dir);
        }
        DBG_PRINT("Using POSIX directory listing");
#endif

        return files;
    }

#if __cplusplus >= 201703L
    static std::optional<std::string> tryGetFile(const std::string& path) {
        // C++17 filesystem and optional
        namespace fs = std::filesystem;
        if (fs::exists(path)) {
            return path;
        }
        return std::nullopt;
    }
#endif

#if __cplusplus >= 202002L
    template<std::ranges::range Range>
    static void processRange(Range&& r) {
        // C++20 ranges and concepts
        for (auto&& item : r) {
            DBG_PRINT("Processing: " << item);
        }
    }
#endif
};

void demonstrateConditionalIncludes() {
    std::string currentDir = PlatformUtils::getCurrentDirectory();
    auto files = PlatformUtils::listDirectory(".");

    DBG_PRINT("Current directory: " << currentDir);
    DBG_PRINT("Found " << files.size() << " files");

#if __cplusplus >= 201703L
    auto maybeFile = PlatformUtils::tryGetFile("test.txt");
    if (maybeFile) {
        DBG_PRINT("Found file: " << *maybeFile);
    }
#endif

#if __cplusplus >= 202002L
    std::vector<int> numbers = {1, 2, 3, 4, 5};
    PlatformUtils::processRange(numbers);
#endif
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    conditional_imports = [
        call
        for call in import_relationships
        if "conditional_includes" in call.args[0][2]
    ]

    assert len(conditional_imports) >= 3, (
        f"Expected at least 3 conditional includes, found {len(conditional_imports)}"
    )

    imported_headers = [call.args[2][2] for call in conditional_imports]
    always_present = ["string", "vector", "memory"]

    for expected in always_present:
        assert any(expected in header for header in imported_headers), (
            f"Missing always-present include: {expected}\nFound: {imported_headers}"
        )


def test_system_vs_local_includes(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test distinction between system <> and local "" includes."""
    test_file = cpp_includes_project / "include_types.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// System includes (angle brackets)
#include <iostream>
#include <vector>
#include <string>
#include <algorithm>
#include <memory>
#include <functional>

// Local includes (quotes)
#include "base.h"
#include "common/types.h"
#include "utils/math.h"

// Mixed usage demonstrating both
#include <map>
#include "local_header.h"
#include <set>
#include "another_local.h"

// Complex local path
#include "../external/third_party.h"
#include "../../global_config.h"

class IncludeDemo {
private:
    // Using system library types
    std::vector<std::string> messages_;
    std::map<ID, std::string> idToName_;
    std::unique_ptr<Base> processor_;

public:
    IncludeDemo() {
        // Using system library functions
        messages_.reserve(100);
        processor_ = std::make_unique<Base>();
    }

    void processData() {
        // Using local header types and functions
        ID currentId = 1;
        Real value = utils::math::add(10.0, 20.0);
        Status status = SUCCESS;

        // Using system algorithms
        std::sort(messages_.begin(), messages_.end());
        auto it = std::find_if(messages_.begin(), messages_.end(),
                              [](const std::string& msg) {
                                  return msg.length() > 5;
                              });

        // Using local functionality
        if (processor_) {
            processor_->process();
        }
    }

    void addMessage(const std::string& message) {
        messages_.push_back(message);

        // Use system container
        std::set<std::string> unique_messages(messages_.begin(), messages_.end());

        // Use local types
        ID messageId = static_cast<ID>(messages_.size());
        idToName_[messageId] = message;
    }
};

// Function using both system and local includes
void demonstrateIncludeTypes() {
    IncludeDemo demo;

    // System library usage
    std::vector<std::string> testMessages = {
        "Hello", "World", "C++", "Includes"
    };

    std::for_each(testMessages.begin(), testMessages.end(),
                  [&demo](const std::string& msg) {
                      demo.addMessage(msg);
                  });

    demo.processData();

    // Local header usage
    Real pi = utils::math::PI;
    Real circumference = utils::math::multiply(2.0 * pi, 5.0);

    Status finalStatus = SUCCESS;
    std::cout << "Demo completed with status: " << finalStatus << std::endl;
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    include_type_imports = [
        call for call in import_relationships if "include_types" in call.args[0][2]
    ]

    assert len(include_type_imports) >= 8, (
        f"Expected at least 8 include relationships, found {len(include_type_imports)}"
    )

    imported_headers = [call.args[2][2] for call in include_type_imports]

    system_headers = ["iostream", "vector", "string", "algorithm", "memory", "map"]
    for expected in system_headers:
        assert any(expected in header for header in imported_headers), (
            f"Missing system include: {expected}\nFound: {imported_headers}"
        )

    local_headers = ["base", "types", "math"]
    for expected in local_headers:
        assert any(expected in header for header in imported_headers), (
            f"Missing local include: {expected}\nFound: {imported_headers}"
        )


def test_include_guards_and_pragma_once(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test handling of include guards and #pragma once."""

    guard_header = cpp_includes_project / "include" / "with_guards.h"
    guard_header.write_text(
        encoding="utf-8",
        data="""
#ifndef WITH_GUARDS_H
#define WITH_GUARDS_H

#include <string>

class GuardedClass {
public:
    std::string getName() const;
    void setName(const std::string& name);
private:
    std::string name_;
};

#endif // WITH_GUARDS_H
""",
    )

    pragma_header = cpp_includes_project / "include" / "with_pragma.h"
    pragma_header.write_text(
        encoding="utf-8",
        data="""
#pragma once

#include <vector>
#include <memory>

class PragmaClass {
public:
    void addItem(int item);
    std::vector<int> getItems() const;

private:
    std::unique_ptr<std::vector<int>> items_;
};
""",
    )

    test_file = cpp_includes_project / "include_guards_test.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Include headers with different guard mechanisms
#include "include/with_guards.h"
#include "include/with_pragma.h"

// Multiple includes of the same header (should be handled by guards)
#include "include/with_guards.h"  // Should be ignored due to include guard
#include "include/with_pragma.h"  // Should be ignored due to #pragma once

// Standard library includes
#include <iostream>
#include <string>

void testIncludeGuards() {
    // Use guarded class
    GuardedClass guarded;
    guarded.setName("Guarded Object");
    std::string name = guarded.getName();

    // Use pragma class
    PragmaClass pragma;
    pragma.addItem(42);
    pragma.addItem(100);
    auto items = pragma.getItems();

    std::cout << "Guarded class name: " << name << std::endl;
    std::cout << "Pragma class items: " << items.size() << std::endl;
}

// Implementation of GuardedClass methods
std::string GuardedClass::getName() const {
    return name_;
}

void GuardedClass::setName(const std::string& name) {
    name_ = name;
}

// Implementation of PragmaClass methods
void PragmaClass::addItem(int item) {
    if (!items_) {
        items_ = std::make_unique<std::vector<int>>();
    }
    items_->push_back(item);
}

std::vector<int> PragmaClass::getItems() const {
    if (!items_) {
        return std::vector<int>();
    }
    return *items_;
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    guard_test_imports = [
        call
        for call in import_relationships
        if "include_guards_test" in call.args[0][2]
    ]

    assert len(guard_test_imports) >= 4, (
        f"Expected at least 4 include relationships, found {len(guard_test_imports)}"
    )

    imported_headers = [call.args[2][2] for call in guard_test_imports]

    expected_headers = ["with_guards", "with_pragma", "iostream", "string"]
    for expected in expected_headers:
        assert any(expected in header for header in imported_headers), (
            f"Missing include: {expected}\nFound: {imported_headers}"
        )


def test_cpp_includes_comprehensive(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all C++ include patterns create proper relationships."""
    test_file = cpp_includes_project / "comprehensive_includes.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every C++ include pattern in one file

// System includes
#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <algorithm>
#include <memory>

// C standard library
#include <cstdio>
#include <cmath>

// Local includes
#include "base.h"
#include "common/types.h"
#include "utils/math.h"

// Relative path includes
#include "../external/third_party.h"

// Conditional includes
#ifdef DEBUG
#include <cassert>
#endif

#if __cplusplus >= 201703L
#include <optional>
#endif

class ComprehensiveIncludeDemo : public Base {
private:
    std::vector<std::string> data_;
    std::map<ID, Real> values_;

public:
    ComprehensiveIncludeDemo() {
        // Using system library
        data_.reserve(100);

        // Using local types
        for (ID i = 1; i <= 10; ++i) {
            values_[i] = utils::math::multiply(i, utils::math::PI);
        }
    }

    void process() override {
        // System library algorithms
        std::sort(data_.begin(), data_.end());

        // C standard library
        printf("Processing %zu items\\n", data_.size());
        double sqrtVal = sqrt(values_.size());

        // Local functionality
        Real sum = 0.0;
        for (const auto& pair : values_) {
            sum = utils::math::add(sum, pair.second);
        }

        // External functionality
        external::thirdPartyFunction();

#ifdef DEBUG
        assert(sum > 0);
#endif

#if __cplusplus >= 201703L
        std::optional<Real> maybeValue = sum > 100 ? std::make_optional(sum) : std::nullopt;
        if (maybeValue) {
            std::cout << "Optional value: " << *maybeValue << std::endl;
        }
#endif
    }

    void addData(const std::string& item) {
        data_.push_back(item);

        // Using standard algorithms
        auto it = std::find(data_.begin(), data_.end(), item);
        if (it != data_.end()) {
            std::cout << "Item already exists at position: "
                      << std::distance(data_.begin(), it) << std::endl;
        }
    }
};

void demonstrateAllIncludes() {
    auto demo = std::make_unique<ComprehensiveIncludeDemo>();

    // Add some test data
    std::vector<std::string> testData = {"item1", "item2", "item3"};
    std::for_each(testData.begin(), testData.end(),
                  [&demo](const std::string& item) {
                      demo->addData(item);
                  });

    // Process the data
    demo->process();

    // Using various included features
    Status status = SUCCESS;
    Real piValue = utils::math::PI;
    ID maxId = 1000;

    std::map<Status, std::string> statusNames = {
        {SUCCESS, "Success"},
        {FAILURE, "Failure"},
        {PENDING, "Pending"}
    };

    std::cout << "Demo completed with status: "
              << statusNames[status] << std::endl;
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_imports = [
        call
        for call in import_relationships
        if "comprehensive_includes" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 10, (
        f"Expected at least 10 comprehensive imports, found {len(comprehensive_imports)}"
    )

    for relationship in comprehensive_imports:
        assert len(relationship.args) == 3, "Import relationship should have 3 args"
        assert relationship.args[1] == "IMPORTS", "Second arg should be 'IMPORTS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive_includes" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target module should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"

    inheritance_found = any(
        "ComprehensiveIncludeDemo" in call.args[0][2] and "Base" in call.args[2][2]
        for call in inherits_relationships
    )
    assert inheritance_found, (
        "Expected inheritance relationship ComprehensiveIncludeDemo -> Base"
    )


def test_cpp20_module_import_syntax(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++20 module import syntax parsing."""
    test_file = cpp_includes_project / "module_imports.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// C++20 module import declarations
import std.core;
import std.io;
import <iostream>;
import <vector>;
import <string>;

// Module implementation unit
module my_module;

// Module partition import
import :partition1;
import :utilities;

// Export module declaration
export module my_export_module;

class ModuleUser {
public:
    void useModules() {
        std::cout << "Using C++20 modules" << std::endl;
    }
};
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    module_imports = [
        call for call in import_relationships if "module_imports" in call.args[0][2]
    ]

    assert len(module_imports) >= 3, (
        f"Expected at least 3 module imports, found {len(module_imports)}"
    )

    imported_modules = [call.args[2][2] for call in module_imports]

    expected_patterns = ["iostream", "vector", "string"]
    for pattern in expected_patterns:
        assert any(pattern in module for module in imported_modules), (
            f"Missing C++20 module import: {pattern}\nFound: {imported_modules}"
        )


def test_cpp20_module_partition_imports(
    cpp_includes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test C++20 module partition import syntax."""
    test_file = cpp_includes_project / "partition_imports.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Primary module interface
export module data_processor;

// Standard library imports
import <vector>;
import <algorithm>;
import <functional>;

// Partition imports
import :core;
import :algorithms;
import :utilities;

export namespace processor {
    class DataProcessor {
    public:
        void process() {
            std::vector<int> data = {1, 2, 3, 4, 5};
            std::sort(data.begin(), data.end());
        }
    };
}
""",
    )

    run_updater(cpp_includes_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    partition_imports = [
        call for call in import_relationships if "partition_imports" in call.args[0][2]
    ]

    assert len(partition_imports) >= 3, (
        f"Expected at least 3 partition imports, found {len(partition_imports)}"
    )

    imported_modules = [call.args[2][2] for call in partition_imports]

    expected_stdlib = ["vector", "algorithm", "functional"]
    for pattern in expected_stdlib:
        assert any(pattern in module for module in imported_modules), (
            f"Missing stdlib import: {pattern}\nFound: {imported_modules}"
        )
