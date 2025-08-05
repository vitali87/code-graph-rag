"""
Comprehensive C++ smart pointers testing.
Tests unique_ptr, shared_ptr, weak_ptr, custom deleters, and various memory management patterns.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def cpp_smart_pointers_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with smart pointer patterns."""
    project_path = temp_repo / "cpp_smart_pointers_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_unique_ptr_patterns(
    cpp_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test unique_ptr usage patterns and ownership semantics."""
    test_file = cpp_smart_pointers_project / "unique_ptr_patterns.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <memory>
#include <vector>
#include <string>
#include <utility>

// Simple resource class
class Resource {
private:
    std::string name_;
    int value_;

public:
    Resource(const std::string& name, int value)
        : name_(name), value_(value) {
        std::cout << "Resource " << name_ << " created" << std::endl;
    }

    ~Resource() {
        std::cout << "Resource " << name_ << " destroyed" << std::endl;
    }

    // Delete copy operations
    Resource(const Resource&) = delete;
    Resource& operator=(const Resource&) = delete;

    // Allow move operations
    Resource(Resource&& other) noexcept
        : name_(std::move(other.name_)), value_(other.value_) {
        other.value_ = 0;
    }

    Resource& operator=(Resource&& other) noexcept {
        if (this != &other) {
            name_ = std::move(other.name_);
            value_ = other.value_;
            other.value_ = 0;
        }
        return *this;
    }

    void use() const {
        std::cout << "Using resource " << name_ << " with value " << value_ << std::endl;
    }

    const std::string& getName() const { return name_; }
    int getValue() const { return value_; }
};

// Factory function returning unique_ptr
std::unique_ptr<Resource> createResource(const std::string& name, int value) {
    return std::make_unique<Resource>(name, value);
}

// Function taking ownership
void takeOwnership(std::unique_ptr<Resource> resource) {
    std::cout << "Taking ownership of " << resource->getName() << std::endl;
    resource->use();
    // Resource will be destroyed when function exits
}

// Function borrowing resource
void borrowResource(const Resource* resource) {
    if (resource) {
        std::cout << "Borrowing " << resource->getName() << std::endl;
        resource->use();
    }
}

// Container managing unique_ptr collection
class ResourceManager {
private:
    std::vector<std::unique_ptr<Resource>> resources_;

public:
    void addResource(std::unique_ptr<Resource> resource) {
        resources_.push_back(std::move(resource));
    }

    void addResource(const std::string& name, int value) {
        resources_.push_back(std::make_unique<Resource>(name, value));
    }

    Resource* getResource(size_t index) {
        if (index < resources_.size()) {
            return resources_[index].get();
        }
        return nullptr;
    }

    std::unique_ptr<Resource> releaseResource(size_t index) {
        if (index < resources_.size()) {
            auto resource = std::move(resources_[index]);
            resources_.erase(resources_.begin() + index);
            return resource;
        }
        return nullptr;
    }

    void clearAll() {
        resources_.clear();
    }

    size_t size() const { return resources_.size(); }

    void useAll() const {
        for (const auto& resource : resources_) {
            if (resource) {
                resource->use();
            }
        }
    }
};

// Custom deleter example
struct FileDeleter {
    void operator()(FILE* file) const {
        if (file) {
            std::cout << "Closing file with custom deleter" << std::endl;
            fclose(file);
        }
    }
};

// Array deleter
template<typename T>
struct ArrayDeleter {
    void operator()(T* array) const {
        std::cout << "Deleting array with custom deleter" << std::endl;
        delete[] array;
    }
};

// Smart pointer wrapper for legacy APIs
class LegacyResourceWrapper {
private:
    std::unique_ptr<void, std::function<void(void*)>> resource_;

public:
    LegacyResourceWrapper(void* resource, std::function<void(void*)> deleter)
        : resource_(resource, deleter) {}

    void* get() const { return resource_.get(); }

    void* release() { return resource_.release(); }
};

void demonstrateUniquePtrBasics() {
    std::cout << "=== Unique Pointer Basics ===" << std::endl;

    // Basic creation and destruction
    {
        auto resource = std::make_unique<Resource>("Basic", 100);
        resource->use();
    } // Resource destroyed here

    // Moving ownership
    auto resource1 = std::make_unique<Resource>("Movable", 200);
    auto resource2 = std::move(resource1);
    // resource1 is now empty
    if (!resource1) {
        std::cout << "resource1 is empty after move" << std::endl;
    }
    resource2->use();

    // Factory pattern
    auto factoryResource = createResource("Factory", 300);
    factoryResource->use();

    // Passing ownership
    auto transferResource = std::make_unique<Resource>("Transfer", 400);
    takeOwnership(std::move(transferResource));
    // transferResource is now empty

    // Borrowing without ownership
    auto borrowedResource = std::make_unique<Resource>("Borrowed", 500);
    borrowResource(borrowedResource.get());
    // borrowedResource still owns the resource

    // Reset and release
    auto resetResource = std::make_unique<Resource>("Reset", 600);
    resetResource.reset(); // Destroys current resource
    resetResource = std::make_unique<Resource>("New", 700);

    Resource* rawPtr = resetResource.release(); // Releases ownership
    delete rawPtr; // Manual cleanup required
}

void demonstrateUniquePtrContainers() {
    std::cout << "=== Unique Pointer Containers ===" << std::endl;

    ResourceManager manager;

    // Add resources
    manager.addResource(std::make_unique<Resource>("First", 1));
    manager.addResource("Second", 2);
    manager.addResource(createResource("Third", 3));

    // Use resources
    manager.useAll();

    // Borrow a resource
    if (auto* resource = manager.getResource(1)) {
        borrowResource(resource);
    }

    // Transfer ownership out
    auto extracted = manager.releaseResource(0);
    if (extracted) {
        std::cout << "Extracted: ";
        extracted->use();
    }

    std::cout << "Remaining resources: " << manager.size() << std::endl;
    manager.useAll();

    // Clear all
    manager.clearAll();
    std::cout << "After clear: " << manager.size() << " resources" << std::endl;
}

void demonstrateCustomDeleters() {
    std::cout << "=== Custom Deleters ===" << std::endl;

    // File with custom deleter
    {
        std::unique_ptr<FILE, FileDeleter> file(
            fopen("test.txt", "w"),
            FileDeleter()
        );

        if (file) {
            fprintf(file.get(), "Hello from unique_ptr\n");
        }
    } // File automatically closed

    // Array with custom deleter
    {
        std::unique_ptr<int, ArrayDeleter<int>> array(
            new int[5]{1, 2, 3, 4, 5},
            ArrayDeleter<int>()
        );

        for (int i = 0; i < 5; ++i) {
            std::cout << "Array[" << i << "] = " << array.get()[i] << std::endl;
        }
    } // Array deleted with custom deleter

    // Lambda deleter
    {
        auto deleter = [](Resource* r) {
            std::cout << "Lambda deleter for " << r->getName() << std::endl;
            delete r;
        };

        std::unique_ptr<Resource, decltype(deleter)> resource(
            new Resource("Lambda", 800),
            deleter
        );

        resource->use();
    }

    // Function deleter
    {
        auto customFree = [](void* ptr) {
            std::cout << "Custom free function" << std::endl;
            free(ptr);
        };

        std::unique_ptr<char, decltype(customFree)> buffer(
            static_cast<char*>(malloc(100)),
            customFree
        );

        if (buffer) {
            strcpy(buffer.get(), "Hello");
            std::cout << "Buffer: " << buffer.get() << std::endl;
        }
    }
}

// Polymorphic unique_ptr
class Base {
public:
    virtual ~Base() = default;
    virtual void process() const = 0;
};

class DerivedA : public Base {
public:
    void process() const override {
        std::cout << "DerivedA processing" << std::endl;
    }
};

class DerivedB : public Base {
public:
    void process() const override {
        std::cout << "DerivedB processing" << std::endl;
    }
};

std::unique_ptr<Base> createPolymorphic(char type) {
    switch (type) {
        case 'A':
            return std::make_unique<DerivedA>();
        case 'B':
            return std::make_unique<DerivedB>();
        default:
            return nullptr;
    }
}

void demonstratePolymorphicUniquePtr() {
    std::cout << "=== Polymorphic Unique Pointers ===" << std::endl;

    std::vector<std::unique_ptr<Base>> polymorphic;

    polymorphic.push_back(createPolymorphic('A'));
    polymorphic.push_back(createPolymorphic('B'));
    polymorphic.push_back(std::make_unique<DerivedA>());

    for (const auto& ptr : polymorphic) {
        if (ptr) {
            ptr->process();
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_smart_pointers_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_smart_pointers_project.name

    # Expected unique_ptr related classes and functions
    expected_entities = [
        f"{project_name}.unique_ptr_patterns.Resource",
        f"{project_name}.unique_ptr_patterns.ResourceManager",
        f"{project_name}.unique_ptr_patterns.FileDeleter",
        f"{project_name}.unique_ptr_patterns.createResource",
        f"{project_name}.unique_ptr_patterns.demonstrateUniquePtrBasics",
    ]

    # Get all node creation calls
    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    function_calls = [call for call in all_calls if call[0][0] == "Function"]

    created_entities = {
        call[0][1]["qualified_name"] for call in class_calls + function_calls
    }

    # Verify expected entities were created
    found_entities = [
        entity for entity in expected_entities if entity in created_entities
    ]
    assert len(found_entities) >= 4, (
        f"Expected at least 4 unique_ptr entities, found {len(found_entities)}: {found_entities}"
    )


def test_shared_ptr_patterns(
    cpp_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test shared_ptr usage patterns and reference counting."""
    test_file = cpp_smart_pointers_project / "shared_ptr_patterns.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <memory>
#include <vector>
#include <map>
#include <thread>
#include <mutex>

// Node for circular reference demonstration
class Node {
private:
    std::string name_;
    std::shared_ptr<Node> next_;
    std::weak_ptr<Node> parent_;  // Use weak_ptr to avoid cycles

public:
    Node(const std::string& name) : name_(name) {
        std::cout << "Node " << name_ << " created" << std::endl;
    }

    ~Node() {
        std::cout << "Node " << name_ << " destroyed" << std::endl;
    }

    void setNext(std::shared_ptr<Node> node) {
        next_ = node;
    }

    void setParent(std::shared_ptr<Node> node) {
        parent_ = node;
    }

    std::shared_ptr<Node> getNext() const { return next_; }

    std::shared_ptr<Node> getParent() const {
        return parent_.lock();  // Convert weak_ptr to shared_ptr
    }

    const std::string& getName() const { return name_; }

    void printChain() const {
        std::cout << name_;
        if (next_) {
            std::cout << " -> ";
            next_->printChain();
        } else {
            std::cout << std::endl;
        }
    }
};

// Observer pattern with weak_ptr
class Subject;

class Observer {
public:
    virtual ~Observer() = default;
    virtual void update(const Subject* subject) = 0;
};

class Subject {
private:
    std::vector<std::weak_ptr<Observer>> observers_;
    std::string state_;
    mutable std::mutex mutex_;

public:
    void attach(std::shared_ptr<Observer> observer) {
        std::lock_guard<std::mutex> lock(mutex_);
        observers_.push_back(observer);
    }

    void setState(const std::string& state) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            state_ = state;
        }
        notify();
    }

    std::string getState() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return state_;
    }

private:
    void notify() {
        std::lock_guard<std::mutex> lock(mutex_);

        // Remove expired observers
        observers_.erase(
            std::remove_if(observers_.begin(), observers_.end(),
                [](const std::weak_ptr<Observer>& wp) { return wp.expired(); }),
            observers_.end()
        );

        // Notify remaining observers
        for (const auto& wp : observers_) {
            if (auto observer = wp.lock()) {
                observer->update(this);
            }
        }
    }
};

class ConcreteObserver : public Observer,
                        public std::enable_shared_from_this<ConcreteObserver> {
private:
    std::string name_;

public:
    ConcreteObserver(const std::string& name) : name_(name) {}

    void update(const Subject* subject) override {
        std::cout << name_ << " received update: " << subject->getState() << std::endl;
    }

    void registerWith(std::shared_ptr<Subject> subject) {
        subject->attach(shared_from_this());
    }
};

// Cache with shared_ptr
template<typename Key, typename Value>
class Cache {
private:
    mutable std::map<Key, std::shared_ptr<Value>> cache_;
    mutable std::mutex mutex_;
    size_t max_size_;

public:
    Cache(size_t max_size = 100) : max_size_(max_size) {}

    void put(const Key& key, std::shared_ptr<Value> value) {
        std::lock_guard<std::mutex> lock(mutex_);

        if (cache_.size() >= max_size_ && cache_.find(key) == cache_.end()) {
            // Remove oldest entry (simplified - just remove first)
            cache_.erase(cache_.begin());
        }

        cache_[key] = value;
    }

    std::shared_ptr<Value> get(const Key& key) const {
        std::lock_guard<std::mutex> lock(mutex_);

        auto it = cache_.find(key);
        if (it != cache_.end()) {
            return it->second;
        }
        return nullptr;
    }

    size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return cache_.size();
    }

    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        cache_.clear();
    }
};

// Thread-safe reference counting
class ThreadSafeResource {
private:
    std::string data_;
    mutable std::mutex mutex_;

public:
    ThreadSafeResource(const std::string& data) : data_(data) {
        std::cout << "ThreadSafeResource created: " << data_ << std::endl;
    }

    ~ThreadSafeResource() {
        std::cout << "ThreadSafeResource destroyed: " << data_ << std::endl;
    }

    void modify(const std::string& newData) {
        std::lock_guard<std::mutex> lock(mutex_);
        data_ = newData;
    }

    std::string read() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return data_;
    }
};

void demonstrateSharedPtrBasics() {
    std::cout << "=== Shared Pointer Basics ===" << std::endl;

    // Basic creation
    auto shared1 = std::make_shared<Node>("First");
    std::cout << "Reference count: " << shared1.use_count() << std::endl;

    // Copy increases reference count
    {
        auto shared2 = shared1;
        std::cout << "Reference count after copy: " << shared1.use_count() << std::endl;

        auto shared3 = shared1;
        std::cout << "Reference count with 3 pointers: " << shared1.use_count() << std::endl;
    } // shared2 and shared3 destroyed, count decreases

    std::cout << "Reference count after scope: " << shared1.use_count() << std::endl;

    // Move doesn't increase count
    auto shared4 = std::make_shared<Node>("Second");
    auto shared5 = std::move(shared4);

    if (!shared4) {
        std::cout << "shared4 is empty after move" << std::endl;
    }
    std::cout << "shared5 count: " << shared5.use_count() << std::endl;

    // Reset
    shared1.reset(); // Decreases count, may destroy object
    std::cout << "After reset, shared1 is " << (shared1 ? "valid" : "empty") << std::endl;
}

void demonstrateCircularReferences() {
    std::cout << "=== Circular References ===" << std::endl;

    // Problem: circular reference with shared_ptr
    {
        auto node1 = std::make_shared<Node>("Circular1");
        auto node2 = std::make_shared<Node>("Circular2");

        // This would create a circular reference if next_ was shared_ptr for both
        // node1->setNext(node2);
        // node2->setNext(node1);  // Memory leak!

        // Solution: use weak_ptr for one direction
        node1->setNext(node2);
        node2->setParent(node1);  // parent_ is weak_ptr

        std::cout << "node1 count: " << node1.use_count() << std::endl;
        std::cout << "node2 count: " << node2.use_count() << std::endl;
    } // Both nodes properly destroyed

    // Linked list with proper cleanup
    {
        auto head = std::make_shared<Node>("Head");
        auto middle = std::make_shared<Node>("Middle");
        auto tail = std::make_shared<Node>("Tail");

        head->setNext(middle);
        middle->setNext(tail);

        // Set parents using weak_ptr
        middle->setParent(head);
        tail->setParent(middle);

        head->printChain();
    } // All nodes properly destroyed
}

void demonstrateObserverPattern() {
    std::cout << "=== Observer Pattern with weak_ptr ===" << std::endl;

    auto subject = std::make_shared<Subject>();

    {
        auto observer1 = std::make_shared<ConcreteObserver>("Observer1");
        auto observer2 = std::make_shared<ConcreteObserver>("Observer2");

        observer1->registerWith(subject);
        observer2->registerWith(subject);

        subject->setState("State A");

        {
            auto observer3 = std::make_shared<ConcreteObserver>("Observer3");
            observer3->registerWith(subject);

            subject->setState("State B");
        } // observer3 destroyed

        subject->setState("State C"); // observer3 automatically removed
    } // observer1 and observer2 destroyed

    subject->setState("State D"); // No observers, but no crash
}

void demonstrateThreadSafety() {
    std::cout << "=== Thread Safety with shared_ptr ===" << std::endl;

    auto resource = std::make_shared<ThreadSafeResource>("Shared Data");

    std::vector<std::thread> threads;

    // Multiple threads sharing the resource
    for (int i = 0; i < 5; ++i) {
        threads.emplace_back([resource, i]() {
            // Each thread has its own shared_ptr copy
            std::cout << "Thread " << i << " count: " << resource.use_count() << std::endl;

            resource->modify("Modified by thread " + std::to_string(i));
            std::this_thread::sleep_for(std::chrono::milliseconds(10));

            std::cout << "Thread " << i << " read: " << resource->read() << std::endl;
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    std::cout << "Final count: " << resource.use_count() << std::endl;
}

void demonstrateCaching() {
    std::cout << "=== Caching with shared_ptr ===" << std::endl;

    Cache<std::string, Node> cache(3);

    // Add items to cache
    cache.put("node1", std::make_shared<Node>("Cached1"));
    cache.put("node2", std::make_shared<Node>("Cached2"));
    cache.put("node3", std::make_shared<Node>("Cached3"));

    // Retrieve from cache
    if (auto node = cache.get("node2")) {
        std::cout << "Retrieved from cache: " << node->getName() << std::endl;
        std::cout << "Reference count: " << node.use_count() << std::endl;
    }

    // Add more items (causes eviction)
    cache.put("node4", std::make_shared<Node>("Cached4"));

    // Try to get evicted item
    if (!cache.get("node1")) {
        std::cout << "node1 was evicted from cache" << std::endl;
    }

    std::cout << "Cache size: " << cache.size() << std::endl;

    // Clear cache
    cache.clear();
    std::cout << "Cache size after clear: " << cache.size() << std::endl;
}

// Custom allocator
template<typename T>
struct CustomAllocator {
    using value_type = T;

    T* allocate(std::size_t n) {
        std::cout << "Custom allocating " << n << " objects" << std::endl;
        return static_cast<T*>(::operator new(n * sizeof(T)));
    }

    void deallocate(T* p, std::size_t n) {
        std::cout << "Custom deallocating " << n << " objects" << std::endl;
        ::operator delete(p);
    }
};

void demonstrateAllocators() {
    std::cout << "=== Custom Allocators ===" << std::endl;

    // Allocate shared
    auto ptr1 = std::allocate_shared<Node>(CustomAllocator<Node>(), "AllocatedNode");
    ptr1->printChain();

    // Array allocation
    auto array = std::shared_ptr<int[]>(new int[5]{1, 2, 3, 4, 5});
    for (int i = 0; i < 5; ++i) {
        std::cout << "Array[" << i << "] = " << array[i] << std::endl;
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_smart_pointers_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_smart_pointers_project.name

    # Expected shared_ptr related classes
    expected_classes = [
        f"{project_name}.shared_ptr_patterns.Node",
        f"{project_name}.shared_ptr_patterns.Observer",
        f"{project_name}.shared_ptr_patterns.Subject",
        f"{project_name}.shared_ptr_patterns.ConcreteObserver",
        f"{project_name}.shared_ptr_patterns.Cache",
    ]

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Verify expected classes were created
    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 4, (
        f"Expected at least 4 shared_ptr classes, found {len(found_classes)}: {found_classes}"
    )


def test_weak_ptr_and_advanced_patterns(
    cpp_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test weak_ptr usage and advanced smart pointer patterns."""
    test_file = cpp_smart_pointers_project / "weak_ptr_advanced.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <memory>
#include <vector>
#include <unordered_map>
#include <functional>

// Tree structure with parent-child relationships
class TreeNode : public std::enable_shared_from_this<TreeNode> {
private:
    std::string name_;
    std::weak_ptr<TreeNode> parent_;
    std::vector<std::shared_ptr<TreeNode>> children_;

public:
    TreeNode(const std::string& name) : name_(name) {
        std::cout << "TreeNode " << name_ << " created" << std::endl;
    }

    ~TreeNode() {
        std::cout << "TreeNode " << name_ << " destroyed" << std::endl;
    }

    void addChild(std::shared_ptr<TreeNode> child) {
        children_.push_back(child);
        child->parent_ = weak_from_this();
    }

    std::shared_ptr<TreeNode> getParent() const {
        return parent_.lock();
    }

    const std::vector<std::shared_ptr<TreeNode>>& getChildren() const {
        return children_;
    }

    std::string getPath() const {
        if (auto p = parent_.lock()) {
            return p->getPath() + "/" + name_;
        }
        return "/" + name_;
    }

    void printTree(int depth = 0) const {
        std::string indent(depth * 2, ' ');
        std::cout << indent << name_ << std::endl;
        for (const auto& child : children_) {
            child->printTree(depth + 1);
        }
    }

    // Safe self-reference
    std::shared_ptr<TreeNode> getSharedPtr() {
        return shared_from_this();
    }

    // Weak self-reference
    std::weak_ptr<TreeNode> weak_from_this() {
        return shared_from_this();
    }
};

// Event system with weak callbacks
class EventManager {
private:
    using Callback = std::function<void(const std::string&)>;
    using WeakCallback = std::pair<std::weak_ptr<void>, Callback>;

    std::unordered_map<std::string, std::vector<WeakCallback>> listeners_;

public:
    void subscribe(const std::string& event,
                  std::shared_ptr<void> owner,
                  Callback callback) {
        listeners_[event].emplace_back(owner, callback);
    }

    void emit(const std::string& event, const std::string& data) {
        auto it = listeners_.find(event);
        if (it == listeners_.end()) return;

        // Clean up expired listeners while emitting
        auto& callbacks = it->second;
        callbacks.erase(
            std::remove_if(callbacks.begin(), callbacks.end(),
                [&data](WeakCallback& wc) {
                    if (auto owner = wc.first.lock()) {
                        wc.second(data);
                        return false;
                    }
                    return true; // Remove expired
                }),
            callbacks.end()
        );
    }

    size_t getListenerCount(const std::string& event) const {
        auto it = listeners_.find(event);
        if (it != listeners_.end()) {
            return it->second.size();
        }
        return 0;
    }
};

// Resource pool with weak references
template<typename T>
class ResourcePool {
private:
    std::vector<std::weak_ptr<T>> available_;
    std::vector<std::shared_ptr<T>> all_resources_;
    std::function<std::shared_ptr<T>()> factory_;

public:
    ResourcePool(std::function<std::shared_ptr<T>()> factory)
        : factory_(factory) {}

    std::shared_ptr<T> acquire() {
        // Try to find an available resource
        for (auto it = available_.begin(); it != available_.end(); ) {
            if (auto resource = it->lock()) {
                available_.erase(it);
                return resource;
            } else {
                // Remove expired weak_ptr
                it = available_.erase(it);
            }
        }

        // Create new resource if none available
        auto resource = factory_();
        all_resources_.push_back(resource);
        return resource;
    }

    void release(std::shared_ptr<T> resource) {
        available_.push_back(resource);
    }

    size_t totalSize() const { return all_resources_.size(); }
    size_t availableSize() const { return available_.size(); }

    void cleanup() {
        // Remove expired resources
        available_.erase(
            std::remove_if(available_.begin(), available_.end(),
                [](const std::weak_ptr<T>& wp) { return wp.expired(); }),
            available_.end()
        );

        all_resources_.erase(
            std::remove_if(all_resources_.begin(), all_resources_.end(),
                [](const std::shared_ptr<T>& sp) { return sp.use_count() == 1; }),
            all_resources_.end()
        );
    }
};

// Cycle detection using weak_ptr
class GraphNode {
private:
    std::string name_;
    std::vector<std::shared_ptr<GraphNode>> strong_edges_;
    std::vector<std::weak_ptr<GraphNode>> weak_edges_;

public:
    GraphNode(const std::string& name) : name_(name) {}

    void addStrongEdge(std::shared_ptr<GraphNode> node) {
        strong_edges_.push_back(node);
    }

    void addWeakEdge(std::shared_ptr<GraphNode> node) {
        weak_edges_.push_back(node);
    }

    bool hasCycleTo(std::shared_ptr<GraphNode> target,
                   std::unordered_set<GraphNode*>& visited) {
        if (this == target.get()) return true;
        if (visited.count(this)) return false;

        visited.insert(this);

        for (const auto& edge : strong_edges_) {
            if (edge->hasCycleTo(target, visited)) return true;
        }

        for (const auto& weak_edge : weak_edges_) {
            if (auto edge = weak_edge.lock()) {
                if (edge->hasCycleTo(target, visited)) return true;
            }
        }

        return false;
    }

    const std::string& getName() const { return name_; }
};

void demonstrateTreeStructure() {
    std::cout << "=== Tree Structure with weak_ptr ===" << std::endl;

    auto root = std::make_shared<TreeNode>("root");
    auto child1 = std::make_shared<TreeNode>("child1");
    auto child2 = std::make_shared<TreeNode>("child2");
    auto grandchild = std::make_shared<TreeNode>("grandchild");

    root->addChild(child1);
    root->addChild(child2);
    child1->addChild(grandchild);

    std::cout << "Tree structure:" << std::endl;
    root->printTree();

    std::cout << "Grandchild path: " << grandchild->getPath() << std::endl;

    // Parent references are weak, no cycles
    std::cout << "Root use count: " << root.use_count() << std::endl;
    std::cout << "Child1 use count: " << child1.use_count() << std::endl;
}

void demonstrateEventSystem() {
    std::cout << "=== Event System with weak_ptr ===" << std::endl;

    EventManager events;

    // Subscriber that will be destroyed
    {
        struct Listener {
            std::string name;
            Listener(const std::string& n) : name(n) {
                std::cout << "Listener " << name << " created" << std::endl;
            }
            ~Listener() {
                std::cout << "Listener " << name << " destroyed" << std::endl;
            }
        };

        auto listener1 = std::make_shared<Listener>("Listener1");
        auto listener2 = std::make_shared<Listener>("Listener2");

        events.subscribe("test_event", listener1,
            [listener1](const std::string& data) {
                std::cout << listener1->name << " received: " << data << std::endl;
            });

        events.subscribe("test_event", listener2,
            [listener2](const std::string& data) {
                std::cout << listener2->name << " received: " << data << std::endl;
            });

        std::cout << "Listeners before: " << events.getListenerCount("test_event") << std::endl;
        events.emit("test_event", "First message");

        // Destroy listener1
        listener1.reset();

        std::cout << "After destroying listener1:" << std::endl;
        events.emit("test_event", "Second message");
        std::cout << "Listeners after: " << events.getListenerCount("test_event") << std::endl;
    }

    // All listeners destroyed
    std::cout << "After scope:" << std::endl;
    events.emit("test_event", "Third message");
    std::cout << "Final listeners: " << events.getListenerCount("test_event") << std::endl;
}

void demonstrateResourcePool() {
    std::cout << "=== Resource Pool with weak_ptr ===" << std::endl;

    struct Connection {
        int id;
        Connection(int i) : id(i) {
            std::cout << "Connection " << id << " created" << std::endl;
        }
        ~Connection() {
            std::cout << "Connection " << id << " destroyed" << std::endl;
        }
        void use() {
            std::cout << "Using connection " << id << std::endl;
        }
    };

    int next_id = 1;
    ResourcePool<Connection> pool(
        [&next_id]() { return std::make_shared<Connection>(next_id++); }
    );

    // Acquire and use resources
    auto conn1 = pool.acquire();
    conn1->use();

    auto conn2 = pool.acquire();
    conn2->use();

    std::cout << "Total: " << pool.totalSize()
              << ", Available: " << pool.availableSize() << std::endl;

    // Release back to pool
    pool.release(conn1);
    std::cout << "After release - Available: " << pool.availableSize() << std::endl;

    // Reuse from pool
    auto conn3 = pool.acquire();
    conn3->use();
    std::cout << "Reused connection (should be 1): " << conn3->id << std::endl;

    // Force cleanup
    conn1.reset();
    conn2.reset();
    conn3.reset();
    pool.cleanup();

    std::cout << "After cleanup - Total: " << pool.totalSize() << std::endl;
}

void demonstrateCycleDetection() {
    std::cout << "=== Cycle Detection ===" << std::endl;

    auto nodeA = std::make_shared<GraphNode>("A");
    auto nodeB = std::make_shared<GraphNode>("B");
    auto nodeC = std::make_shared<GraphNode>("C");
    auto nodeD = std::make_shared<GraphNode>("D");

    // Create graph: A -> B -> C
    //                    ^     |
    //                    |     v
    //                    +---- D (weak edge back to B)
    nodeA->addStrongEdge(nodeB);
    nodeB->addStrongEdge(nodeC);
    nodeC->addStrongEdge(nodeD);
    nodeD->addWeakEdge(nodeB);  // Weak edge prevents memory leak

    // Check for cycles
    std::unordered_set<GraphNode*> visited;
    bool hasCycle = nodeA->hasCycleTo(nodeA, visited);
    std::cout << "Cycle from A to A: " << (hasCycle ? "Yes" : "No") << std::endl;

    std::cout << "Node use counts:" << std::endl;
    std::cout << "A: " << nodeA.use_count() << std::endl;
    std::cout << "B: " << nodeB.use_count() << std::endl;
    std::cout << "C: " << nodeC.use_count() << std::endl;
    std::cout << "D: " << nodeD.use_count() << std::endl;
}

// Advanced patterns
class WeakPtrCache {
private:
    mutable std::unordered_map<std::string, std::weak_ptr<TreeNode>> cache_;

public:
    void store(const std::string& key, std::shared_ptr<TreeNode> node) {
        cache_[key] = node;
    }

    std::shared_ptr<TreeNode> get(const std::string& key) const {
        auto it = cache_.find(key);
        if (it != cache_.end()) {
            return it->second.lock();  // May return nullptr if expired
        }
        return nullptr;
    }

    void cleanup() {
        for (auto it = cache_.begin(); it != cache_.end(); ) {
            if (it->second.expired()) {
                it = cache_.erase(it);
            } else {
                ++it;
            }
        }
    }

    size_t size() const { return cache_.size(); }
};

void demonstrateWeakPtrCache() {
    std::cout << "=== Weak Pointer Cache ===" << std::endl;

    WeakPtrCache cache;

    {
        auto node1 = std::make_shared<TreeNode>("Cached1");
        auto node2 = std::make_shared<TreeNode>("Cached2");

        cache.store("node1", node1);
        cache.store("node2", node2);

        std::cout << "Cache size: " << cache.size() << std::endl;

        // Retrieve while still alive
        if (auto cached = cache.get("node1")) {
            std::cout << "Retrieved: " << cached->getPath() << std::endl;
        }

        // Let node1 go out of scope
        node1.reset();

        // Try to retrieve expired node
        if (!cache.get("node1")) {
            std::cout << "node1 has expired" << std::endl;
        }
    }

    // All nodes out of scope
    cache.cleanup();
    std::cout << "Cache size after cleanup: " << cache.size() << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_smart_pointers_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_smart_pointers_project.name

    # Expected weak_ptr related classes
    expected_classes = [
        f"{project_name}.weak_ptr_advanced.TreeNode",
        f"{project_name}.weak_ptr_advanced.EventManager",
        f"{project_name}.weak_ptr_advanced.ResourcePool",
        f"{project_name}.weak_ptr_advanced.GraphNode",
        f"{project_name}.weak_ptr_advanced.WeakPtrCache",
    ]

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    # Verify expected classes were created
    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 4, (
        f"Expected at least 4 weak_ptr classes, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_smart_pointers_comprehensive(
    cpp_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all smart pointer patterns create proper relationships."""
    test_file = cpp_smart_pointers_project / "comprehensive_smart_pointers.cpp"
    test_file.write_text(
        """
// Every C++ smart pointer pattern in one file
#include <iostream>
#include <memory>
#include <vector>
#include <functional>
#include <thread>

// RAII wrapper using unique_ptr
template<typename T, typename Deleter = std::default_delete<T>>
class RAIIWrapper {
private:
    std::unique_ptr<T, Deleter> resource_;

public:
    template<typename... Args>
    explicit RAIIWrapper(Args&&... args)
        : resource_(std::make_unique<T>(std::forward<Args>(args)...)) {}

    RAIIWrapper(T* resource, Deleter deleter = Deleter())
        : resource_(resource, deleter) {}

    T* get() const { return resource_.get(); }
    T* operator->() const { return resource_.get(); }
    T& operator*() const { return *resource_; }

    T* release() { return resource_.release(); }
    void reset(T* ptr = nullptr) { resource_.reset(ptr); }
};

// Shared ownership container
template<typename T>
class SharedContainer {
private:
    std::vector<std::shared_ptr<T>> items_;

public:
    void add(std::shared_ptr<T> item) {
        items_.push_back(item);
    }

    std::shared_ptr<T> get(size_t index) const {
        if (index < items_.size()) {
            return items_[index];
        }
        return nullptr;
    }

    size_t size() const { return items_.size(); }

    void clear() { items_.clear(); }

    // Allow iteration
    auto begin() { return items_.begin(); }
    auto end() { return items_.end(); }
    auto begin() const { return items_.begin(); }
    auto end() const { return items_.end(); }
};

// Factory with different ownership models
class SmartPointerFactory {
public:
    // Factory returns unique ownership
    template<typename T, typename... Args>
    static std::unique_ptr<T> createUnique(Args&&... args) {
        return std::make_unique<T>(std::forward<Args>(args)...);
    }

    // Factory returns shared ownership
    template<typename T, typename... Args>
    static std::shared_ptr<T> createShared(Args&&... args) {
        return std::make_shared<T>(std::forward<Args>(args)...);
    }

    // Factory with custom deleter
    template<typename T>
    static std::unique_ptr<T, std::function<void(T*)>>
    createWithDeleter(T* ptr, std::function<void(T*)> deleter) {
        return std::unique_ptr<T, std::function<void(T*)>>(ptr, deleter);
    }
};

// Pimpl idiom with unique_ptr
class Widget {
private:
    class Impl;
    std::unique_ptr<Impl> pImpl;

public:
    Widget();
    ~Widget();

    Widget(Widget&&) noexcept;
    Widget& operator=(Widget&&) noexcept;

    void doSomething();
    int getValue() const;
};

// Implementation in cpp file (simulated here)
class Widget::Impl {
private:
    int value_;
    std::string data_;

public:
    Impl() : value_(42), data_("Widget Implementation") {}

    void doSomething() {
        std::cout << "Doing something with " << data_ << std::endl;
    }

    int getValue() const { return value_; }
};

Widget::Widget() : pImpl(std::make_unique<Impl>()) {}
Widget::~Widget() = default;
Widget::Widget(Widget&&) noexcept = default;
Widget& Widget::operator=(Widget&&) noexcept = default;

void Widget::doSomething() { pImpl->doSomething(); }
int Widget::getValue() const { return pImpl->getValue(); }

// Type erasure with shared_ptr
class AnyCallable {
private:
    struct CallableBase {
        virtual ~CallableBase() = default;
        virtual void call() const = 0;
    };

    template<typename F>
    struct CallableImpl : CallableBase {
        F f;
        CallableImpl(F func) : f(std::move(func)) {}
        void call() const override { f(); }
    };

    std::shared_ptr<CallableBase> callable_;

public:
    template<typename F>
    AnyCallable(F f) : callable_(std::make_shared<CallableImpl<F>>(std::move(f))) {}

    void operator()() const {
        if (callable_) {
            callable_->call();
        }
    }
};

// Copy-on-write with shared_ptr
template<typename T>
class COWWrapper {
private:
    mutable std::shared_ptr<T> data_;

    void ensureUnique() {
        if (data_.use_count() > 1) {
            data_ = std::make_shared<T>(*data_);
        }
    }

public:
    COWWrapper(const T& value) : data_(std::make_shared<T>(value)) {}

    const T& read() const { return *data_; }

    T& write() {
        ensureUnique();
        return *data_;
    }

    size_t shareCount() const { return data_.use_count(); }
};

// Demonstrating all patterns
void demonstrateComprehensivePatterns() {
    std::cout << "=== Comprehensive Smart Pointer Patterns ===" << std::endl;

    // RAII wrapper
    {
        RAIIWrapper<int> wrapper(new int(100));
        std::cout << "RAII value: " << *wrapper << std::endl;
    }

    // Custom deleter RAII
    {
        auto fileDeleter = [](FILE* f) {
            if (f) {
                std::cout << "Closing file" << std::endl;
                fclose(f);
            }
        };

        RAIIWrapper<FILE, decltype(fileDeleter)> file(
            fopen("test.txt", "w"), fileDeleter
        );

        if (file.get()) {
            fprintf(file.get(), "RAII file test\n");
        }
    }

    // Shared container
    SharedContainer<std::string> container;
    container.add(std::make_shared<std::string>("First"));
    container.add(std::make_shared<std::string>("Second"));

    for (const auto& item : container) {
        std::cout << "Container item: " << *item << std::endl;
    }

    // Factory patterns
    auto unique_obj = SmartPointerFactory::createUnique<int>(42);
    auto shared_obj = SmartPointerFactory::createShared<double>(3.14);

    auto custom_deleter = [](int* p) {
        std::cout << "Custom deleting int: " << *p << std::endl;
        delete p;
    };

    auto custom_obj = SmartPointerFactory::createWithDeleter(
        new int(99), custom_deleter
    );

    // Pimpl idiom
    Widget widget;
    widget.doSomething();
    std::cout << "Widget value: " << widget.getValue() << std::endl;

    // Type erasure
    AnyCallable callable1([]() { std::cout << "Lambda callable" << std::endl; });
    AnyCallable callable2([]() { std::cout << "Another lambda" << std::endl; });

    callable1();
    callable2();

    // Copy-on-write
    COWWrapper<std::vector<int>> cow1({1, 2, 3, 4, 5});
    COWWrapper<std::vector<int>> cow2 = cow1;  // Shared data

    std::cout << "COW share count: " << cow1.shareCount() << std::endl;

    // Reading doesn't cause copy
    std::cout << "COW1 size: " << cow1.read().size() << std::endl;
    std::cout << "Still sharing: " << cow1.shareCount() << std::endl;

    // Writing causes copy
    cow2.write().push_back(6);
    std::cout << "After write, COW1 shares: " << cow1.shareCount() << std::endl;
    std::cout << "After write, COW2 shares: " << cow2.shareCount() << std::endl;
}

// Thread-safe singleton with shared_ptr
class Singleton {
private:
    static std::shared_ptr<Singleton> instance_;
    static std::mutex mutex_;

    Singleton() {
        std::cout << "Singleton created" << std::endl;
    }

public:
    static std::shared_ptr<Singleton> getInstance() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!instance_) {
            instance_ = std::shared_ptr<Singleton>(new Singleton());
        }
        return instance_;
    }

    void doWork() {
        std::cout << "Singleton working" << std::endl;
    }
};

std::shared_ptr<Singleton> Singleton::instance_;
std::mutex Singleton::mutex_;

// Aliasing constructor demonstration
struct ComplexObject {
    int id;
    std::string name;
    std::vector<int> data;

    ComplexObject(int i, const std::string& n)
        : id(i), name(n), data{1, 2, 3, 4, 5} {}
};

void demonstrateAliasing() {
    std::cout << "=== Aliasing Constructor ===" << std::endl;

    auto obj = std::make_shared<ComplexObject>(1, "Complex");

    // Create shared_ptr to member using aliasing constructor
    std::shared_ptr<int> id_ptr(obj, &obj->id);
    std::shared_ptr<std::string> name_ptr(obj, &obj->name);
    std::shared_ptr<std::vector<int>> data_ptr(obj, &obj->data);

    std::cout << "Object use count: " << obj.use_count() << std::endl;
    std::cout << "ID via alias: " << *id_ptr << std::endl;
    std::cout << "Name via alias: " << *name_ptr << std::endl;

    // Original object kept alive by member pointers
    obj.reset();
    std::cout << "After reset, ID still accessible: " << *id_ptr << std::endl;
}

void comprehensiveDemonstration() {
    demonstrateComprehensivePatterns();

    // Singleton usage
    auto singleton1 = Singleton::getInstance();
    auto singleton2 = Singleton::getInstance();

    std::cout << "Same singleton: " << (singleton1 == singleton2) << std::endl;
    singleton1->doWork();

    demonstrateAliasing();

    // Smart pointer arrays
    auto array_unique = std::make_unique<int[]>(5);
    auto array_shared = std::shared_ptr<int[]>(new int[5]{10, 20, 30, 40, 50});

    for (int i = 0; i < 5; ++i) {
        array_unique[i] = i * i;
        std::cout << "Unique array[" << i << "] = " << array_unique[i] << std::endl;
        std::cout << "Shared array[" << i << "] = " << array_shared[i] << std::endl;
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_smart_pointers_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    call_relationships = [c for c in all_relationships if c.args[1] == "CALLS"]
    defines_relationships = [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive smart pointer coverage
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_smart_pointers" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 8, (
        f"Expected at least 8 comprehensive smart pointer calls, found {len(comprehensive_calls)}"
    )

    # Test that smart pointer parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
    print("✅ C++ smart pointers relationship validation passed:")
    print(f"   - CALLS relationships: {len(call_relationships)}")
    print(f"   - DEFINES relationships: {len(defines_relationships)}")
    print(f"   - Comprehensive smart pointer calls: {len(comprehensive_calls)}")
