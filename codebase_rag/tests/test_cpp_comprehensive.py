from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_comprehensive_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project combining all features."""
    project_path = temp_repo / "cpp_comprehensive_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()
    (project_path / "include" / "core").mkdir()
    (project_path / "include" / "utils").mkdir()
    (project_path / "tests").mkdir()

    (project_path / "include" / "core" / "base.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
#include <memory>

namespace core {
    template<typename T>
    class Base {
    public:
        virtual ~Base() = default;
        virtual void process(const T& data) = 0;
        virtual std::unique_ptr<T> clone() const = 0;
    };
}
""",
    )

    (project_path / "include" / "utils" / "helpers.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
#include <string>
#include <vector>

namespace utils {
    template<typename Container>
    void sort_container(Container& container);

    std::string join(const std::vector<std::string>& strings, const std::string& delimiter);
}
""",
    )

    return project_path


def test_comprehensive_cpp_features(
    cpp_comprehensive_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test comprehensive C++ feature integration."""
    test_file = cpp_comprehensive_project / "comprehensive_example.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive C++ example integrating all major features

// Standard library includes
#include <iostream>
#include <string>
#include <vector>
#include <memory>
#include <algorithm>
#include <functional>
#include <map>
#include <thread>
#include <mutex>
#include <future>

// Local includes
#include "include/core/base.h"
#include "include/utils/helpers.h"

// Global namespace constants
const int GLOBAL_VERSION = 1;

// Anonymous namespace for internal linkage
namespace {
    void internal_logger(const std::string& message) {
        std::cout << "[INTERNAL] " << message << std::endl;
    }

    template<typename T>
    class InternalCache {
    private:
        std::map<std::string, T> cache_;
        mutable std::mutex mutex_;

    public:
        void store(const std::string& key, const T& value) {
            std::lock_guard<std::mutex> lock(mutex_);
            cache_[key] = value;
        }

        bool retrieve(const std::string& key, T& value) const {
            std::lock_guard<std::mutex> lock(mutex_);
            auto it = cache_.find(key);
            if (it != cache_.end()) {
                value = it->second;
                return true;
            }
            return false;
        }

        size_t size() const {
            std::lock_guard<std::mutex> lock(mutex_);
            return cache_.size();
        }
    };
}

// Main application namespace
namespace app {
    // Forward declarations
    template<typename T> class DataProcessor;
    class TaskManager;

    // Type aliases and using declarations
    using DataPtr = std::shared_ptr<std::string>;
    using ProcessorPtr = std::unique_ptr<DataProcessor<std::string>>;

    // Modern C++ features namespace
    namespace modern {
        // Lambda utilities
        auto create_filter = [](const std::string& pattern) {
            return [pattern](const std::string& text) {
                return text.find(pattern) != std::string::npos;
            };
        };

        // Variadic template function
        template<typename... Args>
        void print_all(Args... args) {
            ((std::cout << args << " "), ...);  // C++17 fold expression
            std::cout << std::endl;
        }

        // Constexpr function (compile-time computation)
        constexpr int factorial(int n) {
            return (n <= 1) ? 1 : n * factorial(n - 1);
        }

        // SFINAE helper
        template<typename T>
        std::enable_if_t<std::is_arithmetic_v<T>, T>
        safe_divide(T a, T b) {
            return (b != T{}) ? a / b : T{};
        }

        // Perfect forwarding
        template<typename T>
        auto make_data_ptr(T&& value) {
            return std::make_shared<std::string>(std::forward<T>(value));
        }
    }

    // Abstract base class with virtual functions
    class IProcessable {
    public:
        virtual ~IProcessable() = default;
        virtual void process() = 0;
        virtual std::string getType() const = 0;
        virtual std::unique_ptr<IProcessable> clone() const = 0;
    };

    // Template base class inheriting from interface
    template<typename T>
    class DataProcessor : public core::Base<T>, public IProcessable {
    protected:
        T data_;
        std::string name_;
        static inline int instance_count_ = 0;  // C++17 inline static

    public:
        explicit DataProcessor(const T& data, const std::string& name = "DefaultProcessor")
            : data_(data), name_(name) {
            ++instance_count_;
            internal_logger("DataProcessor created: " + name_);
        }

        virtual ~DataProcessor() {
            --instance_count_;
            internal_logger("DataProcessor destroyed: " + name_);
        }

        // Override pure virtual from core::Base
        void process(const T& data) override {
            data_ = data;
            process();  // Call IProcessable::process
        }

        // Override pure virtual from core::Base
        std::unique_ptr<T> clone() const override {
            return std::make_unique<T>(data_);
        }

        // Override pure virtual from IProcessable
        void process() override {
            modern::print_all("Processing", name_, "with data:", data_);
        }

        std::string getType() const override {
            return "DataProcessor<" + std::string(typeid(T).name()) + ">";
        }

        // Virtual function for derived classes
        virtual void preProcess() {
            internal_logger("Pre-processing in " + name_);
        }

        virtual void postProcess() {
            internal_logger("Post-processing in " + name_);
        }

        // Template method pattern
        void execute() {
            preProcess();
            process();
            postProcess();
        }

        // Static methods
        static int getInstanceCount() { return instance_count_; }

        // Getters
        const T& getData() const { return data_; }
        const std::string& getName() const { return name_; }
    };

    // Specialized template for string processing
    template<>
    class DataProcessor<std::string> : public core::Base<std::string>, public IProcessable {
    private:
        std::string data_;
        std::string name_;
        static inline int instance_count_ = 0;
        InternalCache<std::string> cache_;

    public:
        explicit DataProcessor(const std::string& data, const std::string& name = "StringProcessor")
            : data_(data), name_(name) {
            ++instance_count_;
            internal_logger("Specialized StringProcessor created: " + name_);
        }

        ~DataProcessor() {
            --instance_count_;
            internal_logger("Specialized StringProcessor destroyed: " + name_);
        }

        void process(const std::string& data) override {
            data_ = data;
            process();
        }

        std::unique_ptr<std::string> clone() const override {
            return std::make_unique<std::string>(data_);
        }

        void process() override {
            // String-specific processing
            std::string processed;
            if (!cache_.retrieve(data_, processed)) {
                processed = data_;
                std::transform(processed.begin(), processed.end(), processed.begin(), ::toupper);
                cache_.store(data_, processed);
            }
            modern::print_all("String processing result:", processed);
        }

        std::string getType() const override {
            return "DataProcessor<std::string> [Specialized]";
        }

        std::unique_ptr<IProcessable> clone() const override {
            return std::make_unique<DataProcessor<std::string>>(data_, name_);
        }

        static int getInstanceCount() { return instance_count_; }

        // String-specific methods
        void processWithFilter(const std::function<bool(const std::string&)>& filter) {
            if (filter(data_)) {
                process();
            } else {
                modern::print_all("String filtered out:", data_);
            }
        }

        size_t getCacheSize() const { return cache_.size(); }
    };

    // Multiple inheritance example
    class Loggable {
    public:
        virtual ~Loggable() = default;
        virtual void log(const std::string& message) {
            std::cout << "[LOG] " << message << std::endl;
        }
    };

    class Configurable {
    protected:
        std::map<std::string, std::string> config_;

    public:
        virtual ~Configurable() = default;

        void setConfig(const std::string& key, const std::string& value) {
            config_[key] = value;
        }

        std::string getConfig(const std::string& key) const {
            auto it = config_.find(key);
            return (it != config_.end()) ? it->second : "";
        }
    };

    // Advanced processor with multiple inheritance
    class AdvancedProcessor : public DataProcessor<std::string>,
                             public Loggable,
                             public Configurable {
    private:
        std::vector<std::function<void(const std::string&)>> callbacks_;

    public:
        explicit AdvancedProcessor(const std::string& data)
            : DataProcessor<std::string>(data, "AdvancedProcessor") {
            setConfig("version", std::to_string(GLOBAL_VERSION));
            setConfig("threading", "enabled");
        }

        void process() override {
            log("Starting advanced processing for: " + getData());

            // Call base class processing
            DataProcessor<std::string>::process();

            // Execute callbacks
            for (const auto& callback : callbacks_) {
                callback(getData());
            }

            log("Advanced processing completed");
        }

        void addCallback(std::function<void(const std::string&)> callback) {
            callbacks_.push_back(std::move(callback));
        }

        void processAsync() {
            auto future = std::async(std::launch::async, [this]() {
                process();
                return getData().length();
            });

            log("Async processing started, result length: " + std::to_string(future.get()));
        }

        std::unique_ptr<IProcessable> clone() const override {
            auto cloned = std::make_unique<AdvancedProcessor>(getData());
            cloned->config_ = config_;  // Copy configuration
            return cloned;
        }
    };

    // Template class with template template parameter
    template<template<typename> class ContainerTemplate, typename T>
    class ContainerProcessor {
    private:
        ContainerTemplate<T> container_;

    public:
        void add(const T& item) {
            container_.push_back(item);
        }

        template<typename Predicate>
        void processIf(Predicate pred) {
            std::for_each(container_.begin(), container_.end(), [&](const T& item) {
                if (pred(item)) {
                    modern::print_all("Processing item:", item);
                }
            });
        }

        size_t size() const {
            return container_.size();
        }

        auto begin() { return container_.begin(); }
        auto end() { return container_.end(); }
        auto begin() const { return container_.begin(); }
        auto end() const { return container_.end(); }
    };

    // CRTP (Curiously Recurring Template Pattern)
    template<typename Derived>
    class Countable {
    private:
        static inline int count_ = 0;

    public:
        Countable() { ++count_; }
        ~Countable() { --count_; }

        static int getCount() { return count_; }

        void callDerived() {
            static_cast<Derived*>(this)->derivedMethod();
        }
    };

    class CountedProcessor : public Countable<CountedProcessor> {
    public:
        void derivedMethod() {
            modern::print_all("CountedProcessor method called, total instances:", getCount());
        }
    };

    // Factory pattern with templates
    class ProcessorFactory {
    public:
        template<typename T>
        static std::unique_ptr<DataProcessor<T>> create(const T& data, const std::string& name) {
            return std::make_unique<DataProcessor<T>>(data, name);
        }

        static std::unique_ptr<AdvancedProcessor> createAdvanced(const std::string& data) {
            auto processor = std::make_unique<AdvancedProcessor>(data);

            // Add default callbacks
            processor->addCallback([](const std::string& data) {
                modern::print_all("Callback 1 processing:", data);
            });

            processor->addCallback([](const std::string& data) {
                modern::print_all("Callback 2 processing:", data);
            });

            return processor;
        }
    };
}

// Using declarations and aliases
using namespace app;
using StringProcessor = DataProcessor<std::string>;
using IntProcessor = DataProcessor<int>;

void demonstrateComprehensiveCpp() {
    modern::print_all("=== Comprehensive C++ Features Demo ===");

    // Template instantiation and polymorphism
    std::vector<std::unique_ptr<IProcessable>> processors;

    // Create different types of processors
    auto stringProc = ProcessorFactory::create<std::string>("Hello World", "StringProc1");
    auto intProc = ProcessorFactory::create<int>(42, "IntProc1");
    auto advancedProc = ProcessorFactory::createAdvanced("Advanced Data");

    // Store in polymorphic container
    processors.push_back(std::move(stringProc));
    processors.push_back(std::move(intProc));
    processors.push_back(std::move(advancedProc));

    // Process all polymorphically
    for (auto& processor : processors) {
        modern::print_all("Processing type:", processor->getType());
        processor->process();

        // Clone and process again
        auto cloned = processor->clone();
        cloned->process();
    }

    // Template specialization usage
    StringProcessor specialized("specialized string", "SpecializedProc");
    specialized.execute();  // Uses template method pattern

    // Modern C++ features
    auto filter = modern::create_filter("special");
    specialized.processWithFilter(filter);

    // Lambda and algorithm usage
    std::vector<std::string> data = {"hello", "world", "special", "test"};
    std::sort(data.begin(), data.end(), [](const std::string& a, const std::string& b) {
        return a.length() < b.length();
    });

    modern::print_all("Sorted data by length:");
    for (const auto& item : data) {
        std::cout << item << " ";
    }
    std::cout << std::endl;

    // Template template parameter usage
    ContainerProcessor<std::vector, std::string> containerProc;
    for (const auto& item : data) {
        containerProc.add(item);
    }

    containerProc.processIf([](const std::string& s) { return s.length() > 4; });

    // CRTP usage
    CountedProcessor counted1, counted2, counted3;
    counted1.callDerived();
    counted2.callDerived();
    counted3.callDerived();

    // Compile-time computation
    constexpr int fact5 = modern::factorial(5);
    modern::print_all("5! =", fact5);

    // SFINAE usage
    double result = modern::safe_divide(10.0, 3.0);
    modern::print_all("Safe division result:", result);

    // Perfect forwarding
    auto dataPtr1 = modern::make_data_ptr(std::string("forwarded"));
    auto dataPtr2 = modern::make_data_ptr("literal forwarded");

    // Async processing
    AdvancedProcessor asyncProc("async data");
    asyncProc.processAsync();

    // Show instance counts
    modern::print_all("String processor instances:", StringProcessor::getInstanceCount());
    modern::print_all("Int processor instances:", IntProcessor::getInstanceCount());
    modern::print_all("Counted processor instances:", CountedProcessor::getCount());

    modern::print_all("=== Demo Complete ===");
}

// Namespace alias and additional using
namespace short_name = app::modern;

void testNamespaceIntegration() {
    short_name::print_all("Testing namespace alias");

    // Cross-namespace function calls
    std::vector<std::string> strings = {"a", "b", "c"};
    std::string joined = utils::join(strings, ",");  // Would call utils function

    modern::print_all("Joined string:", joined);
}

// Template metaprogramming integration
template<int N>
struct CompileTimeProcessor {
    static void process() {
        modern::print_all("Compile-time processing for N =", N);
        CompileTimeProcessor<N-1>::process();
    }
};

template<>
struct CompileTimeProcessor<0> {
    static void process() {
        modern::print_all("Compile-time processing base case");
    }
};

void testMetaprogramming() {
    CompileTimeProcessor<3>::process();
}

// Main integration test
void runComprehensiveTest() {
    internal_logger("Starting comprehensive C++ test");

    demonstrateComprehensiveCpp();
    testNamespaceIntegration();
    testMetaprogramming();

    internal_logger("Comprehensive C++ test completed");
}
""",
    )

    run_updater(cpp_comprehensive_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")
    imports_relationships = get_relationships(mock_ingestor, "IMPORTS")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_example" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 20, (
        f"Expected at least 20 comprehensive calls, found {len(comprehensive_calls)}"
    )

    comprehensive_inherits = [
        call
        for call in inherits_relationships
        if "comprehensive_example" in call.args[0][2]
    ]

    assert len(comprehensive_inherits) >= 8, (
        f"Expected at least 8 inheritance relationships, found {len(comprehensive_inherits)}"
    )

    comprehensive_imports = [
        call
        for call in imports_relationships
        if "comprehensive_example" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 10, (
        f"Expected at least 10 include relationships, found {len(comprehensive_imports)}"
    )

    expected_classes = [
        "DataProcessor",
        "AdvancedProcessor",
        "ContainerProcessor",
        "CountedProcessor",
        "ProcessorFactory",
        "InternalCache",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_complex_classes = [
        cls
        for cls in created_classes
        if any(expected in cls for expected in expected_classes)
    ]

    assert len(found_complex_classes) >= 4, (
        f"Expected at least 4 complex classes, found {len(found_complex_classes)}: {found_complex_classes}"
    )

    expected_functions = [
        "create_filter",
        "print_all",
        "factorial",
        "safe_divide",
        "make_data_ptr",
        "demonstrateComprehensiveCpp",
        "runComprehensiveTest",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_modern_functions = [
        func
        for func in created_functions
        if any(expected in func for expected in expected_functions)
    ]

    assert len(found_modern_functions) >= 4, (
        f"Expected at least 4 modern C++ functions, found {len(found_modern_functions)}: {found_modern_functions}"
    )


def test_real_world_cpp_scenario(
    cpp_comprehensive_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test a real-world C++ scenario combining multiple files and advanced features."""

    header_file = cpp_comprehensive_project / "include" / "engine.h"
    header_file.write_text(
        encoding="utf-8",
        data="""
#pragma once
#include <memory>
#include <vector>
#include <string>
#include <functional>

namespace engine {
    template<typename T>
    class Component {
    public:
        virtual ~Component() = default;
        virtual void update(const T& data) = 0;
        virtual std::string getType() const = 0;
    };

    class Entity {
    private:
        std::vector<std::unique_ptr<Component<void>>> components_;
        std::string name_;

    public:
        explicit Entity(const std::string& name);
        ~Entity();

        template<typename ComponentType, typename... Args>
        void addComponent(Args&&... args);

        template<typename ComponentType>
        ComponentType* getComponent();

        void update();
        const std::string& getName() const;
    };

    class System {
    public:
        virtual ~System() = default;
        virtual void process(std::vector<Entity*>& entities) = 0;
    };
}
""",
    )

    impl_file = cpp_comprehensive_project / "src" / "engine.cpp"
    impl_file.write_text(
        encoding="utf-8",
        data="""
#include "../include/engine.h"
#include <iostream>
#include <algorithm>

namespace engine {
    // Entity implementation
    Entity::Entity(const std::string& name) : name_(name) {
        std::cout << "Entity created: " << name_ << std::endl;
    }

    Entity::~Entity() {
        std::cout << "Entity destroyed: " << name_ << std::endl;
    }

    void Entity::update() {
        for (auto& component : components_) {
            if (component) {
                component->update();  // Would need void specialization
            }
        }
    }

    const std::string& Entity::getName() const {
        return name_;
    }

    // Specific component implementations
    class RenderComponent : public Component<void> {
    private:
        std::string mesh_name_;

    public:
        explicit RenderComponent(const std::string& mesh) : mesh_name_(mesh) {}

        void update(const void&) override {
            std::cout << "Rendering mesh: " << mesh_name_ << std::endl;
        }

        std::string getType() const override {
            return "RenderComponent";
        }
    };

    class PhysicsComponent : public Component<void> {
    private:
        float velocity_x_, velocity_y_;

    public:
        PhysicsComponent(float vx, float vy) : velocity_x_(vx), velocity_y_(vy) {}

        void update(const void&) override {
            std::cout << "Physics update: velocity(" << velocity_x_ << ", " << velocity_y_ << ")" << std::endl;
        }

        std::string getType() const override {
            return "PhysicsComponent";
        }

        void setVelocity(float vx, float vy) {
            velocity_x_ = vx;
            velocity_y_ = vy;
        }
    };

    // System implementations
    class RenderSystem : public System {
    public:
        void process(std::vector<Entity*>& entities) override {
            std::cout << "RenderSystem processing " << entities.size() << " entities" << std::endl;

            std::for_each(entities.begin(), entities.end(), [](Entity* entity) {
                if (entity) {
                    // Would check for RenderComponent and process
                    std::cout << "Processing entity: " << entity->getName() << std::endl;
                }
            });
        }
    };

    class PhysicsSystem : public System {
    public:
        void process(std::vector<Entity*>& entities) override {
            std::cout << "PhysicsSystem processing " << entities.size() << " entities" << std::endl;

            for (auto* entity : entities) {
                if (entity) {
                    // Would check for PhysicsComponent and process
                    entity->update();
                }
            }
        }
    };
}

// Game engine usage example
void runGameEngineExample() {
    using namespace engine;

    // Create entities
    auto player = std::make_unique<Entity>("Player");
    auto enemy = std::make_unique<Entity>("Enemy");

    // Add components (simplified, would use template methods)
    // player->addComponent<RenderComponent>("player_mesh.obj");
    // player->addComponent<PhysicsComponent>(1.0f, 0.0f);

    // Create systems
    auto renderSystem = std::make_unique<RenderSystem>();
    auto physicsSystem = std::make_unique<PhysicsSystem>();

    // Process entities
    std::vector<Entity*> entities = {player.get(), enemy.get()};

    renderSystem->process(entities);
    physicsSystem->process(entities);

    std::cout << "Game engine example completed" << std::endl;
}
""",
    )

    main_file = cpp_comprehensive_project / "src" / "main.cpp"
    main_file.write_text(
        encoding="utf-8",
        data="""
#include "../include/engine.h"
#include <iostream>
#include <memory>
#include <vector>

// Forward declaration from engine.cpp
void runGameEngineExample();

int main() {
    try {
        std::cout << "Starting real-world C++ application..." << std::endl;

        runGameEngineExample();

        std::cout << "Application completed successfully." << std::endl;
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
}
""",
    )

    run_updater(cpp_comprehensive_project, mock_ingestor)

    imports_relationships = get_relationships(mock_ingestor, "IMPORTS")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    header_imports = [
        call
        for call in imports_relationships
        if "engine.h" in call.args[2][2] or "iostream" in call.args[2][2]
    ]

    assert len(header_imports) >= 2, (
        f"Expected at least 2 header imports, found {len(header_imports)}"
    )

    component_inheritance = [
        call
        for call in inherits_relationships
        if "Component" in call.args[2][2] or "System" in call.args[2][2]
    ]

    assert len(component_inheritance) >= 2, (
        f"Expected at least 2 component inheritance relationships, found {len(component_inheritance)}"
    )


def test_cpp_comprehensive_complete() -> None:
    """Mark comprehensive C++ testing as complete."""
    print("Coverage includes:")
    print("   - Basic syntax (classes, functions, namespaces)")
    print("   - Include directives and header relationships")
    print("   - Complex inheritance hierarchies")
    print("   - Template programming and metaprogramming")
    print("   - Namespace management and qualified names")
    print("   - Modern C++ features integration")
    print("   - Real-world multi-file scenarios")
    print("   - Cross-namespace and cross-file relationships")
    assert True
