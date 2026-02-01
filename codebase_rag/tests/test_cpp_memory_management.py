from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_memory_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with memory management patterns."""
    project_path = temp_repo / "cpp_memory_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_smart_pointers(
    cpp_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test smart pointers: unique_ptr, shared_ptr, weak_ptr."""
    test_file = cpp_memory_project / "smart_pointers.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <vector>
#include <string>

// Resource class for testing smart pointers
class Resource {
private:
    std::string name_;
    int id_;
    static int next_id_;

public:
    Resource(const std::string& name) : name_(name), id_(next_id_++) {
        std::cout << "Resource created: " << name_ << " (ID: " << id_ << ")" << std::endl;
    }

    ~Resource() {
        std::cout << "Resource destroyed: " << name_ << " (ID: " << id_ << ")" << std::endl;
    }

    void doWork() const {
        std::cout << "Resource " << name_ << " is working..." << std::endl;
    }

    const std::string& getName() const { return name_; }
    int getId() const { return id_; }
};

int Resource::next_id_ = 1;

// Factory for creating resources
class ResourceFactory {
public:
    static std::unique_ptr<Resource> createUnique(const std::string& name) {
        return std::make_unique<Resource>(name);
    }

    static std::shared_ptr<Resource> createShared(const std::string& name) {
        return std::make_shared<Resource>(name);
    }

    // Factory with custom deleter
    static std::unique_ptr<Resource, void(*)(Resource*)> createWithCustomDeleter(const std::string& name) {
        auto deleter = [](Resource* r) {
            std::cout << "Custom deleter called for: " << r->getName() << std::endl;
            delete r;
        };
        return std::unique_ptr<Resource, void(*)(Resource*)>(new Resource(name), deleter);
    }
};

// Manager using unique_ptr for exclusive ownership
class UniqueResourceManager {
private:
    std::vector<std::unique_ptr<Resource>> resources_;

public:
    void addResource(std::unique_ptr<Resource> resource) {
        std::cout << "Adding unique resource: " << resource->getName() << std::endl;
        resources_.push_back(std::move(resource));
    }

    std::unique_ptr<Resource> removeResource(const std::string& name) {
        auto it = std::find_if(resources_.begin(), resources_.end(),
            [&name](const std::unique_ptr<Resource>& r) {
                return r->getName() == name;
            });

        if (it != resources_.end()) {
            std::unique_ptr<Resource> resource = std::move(*it);
            resources_.erase(it);
            std::cout << "Removed unique resource: " << name << std::endl;
            return resource;
        }
        return nullptr;
    }

    void processAllResources() const {
        std::cout << "Processing " << resources_.size() << " unique resources:" << std::endl;
        for (const auto& resource : resources_) {
            resource->doWork();
        }
    }

    size_t getResourceCount() const { return resources_.size(); }
};

// Manager using shared_ptr for shared ownership
class SharedResourceManager {
private:
    std::vector<std::shared_ptr<Resource>> resources_;
    std::vector<std::weak_ptr<Resource>> observers_;

public:
    void addResource(std::shared_ptr<Resource> resource) {
        std::cout << "Adding shared resource: " << resource->getName()
                  << " (ref count: " << resource.use_count() << ")" << std::endl;
        resources_.push_back(resource);
    }

    void addObserver(std::weak_ptr<Resource> observer) {
        observers_.push_back(observer);
    }

    std::shared_ptr<Resource> getResource(const std::string& name) const {
        auto it = std::find_if(resources_.begin(), resources_.end(),
            [&name](const std::shared_ptr<Resource>& r) {
                return r->getName() == name;
            });

        return (it != resources_.end()) ? *it : nullptr;
    }

    void removeResource(const std::string& name) {
        auto it = std::find_if(resources_.begin(), resources_.end(),
            [&name](const std::shared_ptr<Resource>& r) {
                return r->getName() == name;
            });

        if (it != resources_.end()) {
            std::cout << "Removing shared resource: " << name
                      << " (ref count before removal: " << (*it).use_count() << ")" << std::endl;
            resources_.erase(it);
        }
    }

    void processAllResources() const {
        std::cout << "Processing " << resources_.size() << " shared resources:" << std::endl;
        for (const auto& resource : resources_) {
            std::cout << "  Resource: " << resource->getName()
                      << " (ref count: " << resource.use_count() << ")" << std::endl;
            resource->doWork();
        }
    }

    void checkObservers() const {
        std::cout << "Checking " << observers_.size() << " observers:" << std::endl;
        for (const auto& weak_ref : observers_) {
            if (auto resource = weak_ref.lock()) {
                std::cout << "  Observer valid: " << resource->getName() << std::endl;
            } else {
                std::cout << "  Observer expired" << std::endl;
            }
        }
    }

    size_t getResourceCount() const { return resources_.size(); }
};

// Cache using weak_ptr to avoid circular dependencies
class ResourceCache {
private:
    std::vector<std::weak_ptr<Resource>> cached_resources_;

public:
    void cacheResource(std::shared_ptr<Resource> resource) {
        cached_resources_.push_back(resource);
        std::cout << "Cached resource: " << resource->getName() << std::endl;
    }

    std::shared_ptr<Resource> getCachedResource(const std::string& name) {
        for (auto it = cached_resources_.begin(); it != cached_resources_.end(); ) {
            if (auto resource = it->lock()) {
                if (resource->getName() == name) {
                    std::cout << "Cache hit: " << name << std::endl;
                    return resource;
                }
                ++it;
            } else {
                // Remove expired weak_ptr
                std::cout << "Removing expired cache entry" << std::endl;
                it = cached_resources_.erase(it);
            }
        }
        std::cout << "Cache miss: " << name << std::endl;
        return nullptr;
    }

    void cleanupExpiredEntries() {
        auto original_size = cached_resources_.size();
        cached_resources_.erase(
            std::remove_if(cached_resources_.begin(), cached_resources_.end(),
                [](const std::weak_ptr<Resource>& weak_ref) {
                    return weak_ref.expired();
                }),
            cached_resources_.end()
        );

        auto removed = original_size - cached_resources_.size();
        if (removed > 0) {
            std::cout << "Cleaned up " << removed << " expired cache entries" << std::endl;
        }
    }

    size_t getCacheSize() const { return cached_resources_.size(); }
};

void testUniquePtr() {
    std::cout << "=== Testing unique_ptr ===" << std::endl;

    UniqueResourceManager manager;

    // Create and add resources
    auto resource1 = ResourceFactory::createUnique("UniqueResource1");
    auto resource2 = ResourceFactory::createUnique("UniqueResource2");
    auto resource3 = ResourceFactory::createUnique("UniqueResource3");

    manager.addResource(std::move(resource1));
    manager.addResource(std::move(resource2));
    manager.addResource(std::move(resource3));

    // Process resources
    manager.processAllResources();

    // Move resource out of manager
    auto moved_resource = manager.removeResource("UniqueResource2");
    if (moved_resource) {
        std::cout << "Successfully moved out resource: " << moved_resource->getName() << std::endl;
        moved_resource->doWork();
    }

    std::cout << "Manager now has " << manager.getResourceCount() << " resources" << std::endl;

    // Test custom deleter
    {
        auto custom_resource = ResourceFactory::createWithCustomDeleter("CustomDeleterResource");
        custom_resource->doWork();
        // Custom deleter called automatically when going out of scope
    }
}

void testSharedPtr() {
    std::cout << "=== Testing shared_ptr ===" << std::endl;

    SharedResourceManager manager;
    ResourceCache cache;

    // Create shared resources
    auto shared1 = ResourceFactory::createShared("SharedResource1");
    auto shared2 = ResourceFactory::createShared("SharedResource2");

    std::cout << "Initial ref count for shared1: " << shared1.use_count() << std::endl;

    // Add to manager (increases ref count)
    manager.addResource(shared1);
    manager.addResource(shared2);

    std::cout << "Ref count after adding to manager: " << shared1.use_count() << std::endl;

    // Cache resources (adds weak references)
    cache.cacheResource(shared1);
    cache.cacheResource(shared2);

    std::cout << "Ref count after caching (weak_ptr doesn't increase count): "
              << shared1.use_count() << std::endl;

    // Create additional shared references
    {
        auto another_ref = shared1;
        std::cout << "Ref count with additional reference: " << shared1.use_count() << std::endl;

        auto retrieved = manager.getResource("SharedResource1");
        std::cout << "Ref count after retrieval: " << shared1.use_count() << std::endl;
    } // additional references go out of scope

    std::cout << "Ref count after scope exit: " << shared1.use_count() << std::endl;

    // Process resources
    manager.processAllResources();

    // Test cache functionality
    auto cached = cache.getCachedResource("SharedResource1");
    if (cached) {
        std::cout << "Retrieved from cache: " << cached->getName() << std::endl;
    }

    // Remove from manager
    manager.removeResource("SharedResource1");
    std::cout << "Ref count after manager removal: " << shared1.use_count() << std::endl;

    // shared1 still exists in our local scope
    shared1->doWork();
}

void testWeakPtr() {
    std::cout << "=== Testing weak_ptr ===" << std::endl;

    ResourceCache cache;
    std::vector<std::weak_ptr<Resource>> observers;

    {
        SharedResourceManager temp_manager;

        // Create shared resources
        auto resource1 = ResourceFactory::createShared("WeakResource1");
        auto resource2 = ResourceFactory::createShared("WeakResource2");

        // Add observers (weak references)
        observers.push_back(resource1);
        observers.push_back(resource2);
        temp_manager.addObserver(resource1);
        temp_manager.addObserver(resource2);

        // Cache resources
        cache.cacheResource(resource1);
        cache.cacheResource(resource2);

        // Add to manager
        temp_manager.addResource(resource1);
        temp_manager.addResource(resource2);

        std::cout << "Resources created and managed (scope 1)" << std::endl;
        temp_manager.checkObservers();

        // Remove one resource from manager
        temp_manager.removeResource("WeakResource1");
        temp_manager.checkObservers();

    } // temp_manager goes out of scope, resources destroyed

    std::cout << "After temp_manager destruction:" << std::endl;

    // Check observers - should be expired
    for (const auto& weak_ref : observers) {
        if (auto resource = weak_ref.lock()) {
            std::cout << "Observer still valid: " << resource->getName() << std::endl;
        } else {
            std::cout << "Observer expired (resource destroyed)" << std::endl;
        }
    }

    // Cleanup cache
    cache.cleanupExpiredEntries();
    std::cout << "Cache size after cleanup: " << cache.getCacheSize() << std::endl;
}

void testCircularReferenceAvoidance() {
    std::cout << "=== Testing Circular Reference Avoidance ===" << std::endl;

    // Demonstrate how weak_ptr prevents circular references
    struct Node {
        std::string name;
        std::shared_ptr<Node> parent;
        std::vector<std::shared_ptr<Node>> children;
        std::weak_ptr<Node> weak_parent; // Use weak_ptr to break cycles

        Node(const std::string& n) : name(n) {
            std::cout << "Node created: " << name << std::endl;
        }

        ~Node() {
            std::cout << "Node destroyed: " << name << std::endl;
        }

        void addChild(std::shared_ptr<Node> child) {
            children.push_back(child);
            child->weak_parent = shared_from_this();
            std::cout << "Added child " << child->name << " to " << name << std::endl;
        }

        void printHierarchy(int level = 0) const {
            std::string indent(level * 2, ' ');
            std::cout << indent << "Node: " << name << " (ref count: "
                      << shared_from_this().use_count() << ")" << std::endl;

            for (const auto& child : children) {
                child->printHierarchy(level + 1);
            }
        }
    };

    // Enable shared_from_this
    struct TreeNode : public std::enable_shared_from_this<TreeNode> {
        std::string name;
        std::vector<std::shared_ptr<TreeNode>> children;
        std::weak_ptr<TreeNode> parent;

        TreeNode(const std::string& n) : name(n) {
            std::cout << "TreeNode created: " << name << std::endl;
        }

        ~TreeNode() {
            std::cout << "TreeNode destroyed: " << name << std::endl;
        }

        void addChild(std::shared_ptr<TreeNode> child) {
            children.push_back(child);
            child->parent = shared_from_this();
            std::cout << "TreeNode " << name << " adopted " << child->name << std::endl;
        }

        std::shared_ptr<TreeNode> getParent() const {
            return parent.lock();
        }
    };

    {
        auto root = std::make_shared<TreeNode>("Root");
        auto child1 = std::make_shared<TreeNode>("Child1");
        auto child2 = std::make_shared<TreeNode>("Child2");
        auto grandchild = std::make_shared<TreeNode>("Grandchild");

        root->addChild(child1);
        root->addChild(child2);
        child1->addChild(grandchild);

        std::cout << "Tree structure created" << std::endl;
        std::cout << "Root ref count: " << root.use_count() << std::endl;
        std::cout << "Child1 ref count: " << child1.use_count() << std::endl;

        // Demonstrate parent access through weak_ptr
        auto parent_of_grandchild = grandchild->getParent();
        if (parent_of_grandchild) {
            std::cout << "Grandchild's parent: " << parent_of_grandchild->name << std::endl;
        }

    } // All shared_ptrs go out of scope, proper cleanup occurs

    std::cout << "Tree destruction completed" << std::endl;
}

void demonstrateSmartPointers() {
    testUniquePtr();
    testSharedPtr();
    testWeakPtr();
    testCircularReferenceAvoidance();
}
""",
    )

    run_updater(cpp_memory_project, mock_ingestor)

    project_name = cpp_memory_project.name

    expected_classes = [
        f"{project_name}.smart_pointers.Resource",
        f"{project_name}.smart_pointers.ResourceFactory",
        f"{project_name}.smart_pointers.UniqueResourceManager",
        f"{project_name}.smart_pointers.SharedResourceManager",
        f"{project_name}.smart_pointers.ResourceCache",
    ]

    expected_functions = [
        f"{project_name}.smart_pointers.testUniquePtr",
        f"{project_name}.smart_pointers.testSharedPtr",
        f"{project_name}.smart_pointers.testWeakPtr",
        f"{project_name}.smart_pointers.demonstrateSmartPointers",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_move_semantics(
    cpp_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test move semantics and perfect forwarding."""
    test_file = cpp_memory_project / "move_semantics.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <string>
#include <utility>
#include <memory>

// Movable resource class
class MovableResource {
private:
    std::unique_ptr<int[]> data_;
    size_t size_;
    std::string name_;

public:
    // Constructor
    MovableResource(const std::string& name, size_t size)
        : name_(name), size_(size) {
        data_ = std::make_unique<int[]>(size);
        for (size_t i = 0; i < size_; ++i) {
            data_[i] = static_cast<int>(i);
        }
        std::cout << "MovableResource created: " << name_ << " (size: " << size_ << ")" << std::endl;
    }

    // Destructor
    ~MovableResource() {
        std::cout << "MovableResource destroyed: " << name_ << std::endl;
    }

    // Copy constructor (expensive)
    MovableResource(const MovableResource& other)
        : name_(other.name_ + "_copy"), size_(other.size_) {
        data_ = std::make_unique<int[]>(size_);
        std::copy(other.data_.get(), other.data_.get() + size_, data_.get());
        std::cout << "MovableResource copied: " << name_ << " (expensive operation)" << std::endl;
    }

    // Copy assignment operator
    MovableResource& operator=(const MovableResource& other) {
        if (this != &other) {
            name_ = other.name_ + "_assigned";
            size_ = other.size_;
            data_ = std::make_unique<int[]>(size_);
            std::copy(other.data_.get(), other.data_.get() + size_, data_.get());
            std::cout << "MovableResource copy-assigned: " << name_ << " (expensive operation)" << std::endl;
        }
        return *this;
    }

    // Move constructor (efficient)
    MovableResource(MovableResource&& other) noexcept
        : data_(std::move(other.data_)), size_(other.size_), name_(std::move(other.name_)) {
        other.size_ = 0;
        name_ += "_moved";
        std::cout << "MovableResource moved: " << name_ << " (efficient operation)" << std::endl;
    }

    // Move assignment operator
    MovableResource& operator=(MovableResource&& other) noexcept {
        if (this != &other) {
            data_ = std::move(other.data_);
            size_ = other.size_;
            name_ = std::move(other.name_) + "_move_assigned";
            other.size_ = 0;
            std::cout << "MovableResource move-assigned: " << name_ << " (efficient operation)" << std::endl;
        }
        return *this;
    }

    // Utility methods
    const std::string& getName() const { return name_; }
    size_t getSize() const { return size_; }
    bool isValid() const { return data_ != nullptr && size_ > 0; }

    void printData() const {
        if (isValid()) {
            std::cout << "Data in " << name_ << ": ";
            for (size_t i = 0; i < std::min(size_, size_t(5)); ++i) {
                std::cout << data_[i] << " ";
            }
            if (size_ > 5) std::cout << "...";
            std::cout << " (total: " << size_ << " elements)" << std::endl;
        } else {
            std::cout << "Resource " << name_ << " is not valid" << std::endl;
        }
    }
};

// Container that demonstrates move semantics
class ResourceContainer {
private:
    std::vector<MovableResource> resources_;

public:
    // Add resource by copy
    void addResourceByCopy(const MovableResource& resource) {
        std::cout << "Adding resource by copy..." << std::endl;
        resources_.push_back(resource); // Copy constructor called
    }

    // Add resource by move
    void addResourceByMove(MovableResource&& resource) {
        std::cout << "Adding resource by move..." << std::endl;
        resources_.push_back(std::move(resource)); // Move constructor called
    }

    // Create and add resource in-place
    template<typename... Args>
    void emplaceResource(Args&&... args) {
        std::cout << "Emplacing resource..." << std::endl;
        resources_.emplace_back(std::forward<Args>(args)...); // Perfect forwarding
    }

    // Return resource by value (move semantics)
    MovableResource removeResource(size_t index) {
        if (index < resources_.size()) {
            MovableResource resource = std::move(resources_[index]);
            resources_.erase(resources_.begin() + index);
            return resource; // Move constructor called for return
        }
        throw std::out_of_range("Invalid index");
    }

    void printAllResources() const {
        std::cout << "Container has " << resources_.size() << " resources:" << std::endl;
        for (size_t i = 0; i < resources_.size(); ++i) {
            std::cout << "  [" << i << "] ";
            resources_[i].printData();
        }
    }

    size_t size() const { return resources_.size(); }
};

// Factory functions demonstrating move semantics
class ResourceFactory {
public:
    // Factory function returning by value (move semantics)
    static MovableResource createResource(const std::string& name, size_t size) {
        std::cout << "Factory creating resource..." << std::endl;
        return MovableResource(name, size); // RVO or move constructor
    }

    // Factory function with move semantics for parameters
    static std::unique_ptr<MovableResource> createUniqueResource(std::string name, size_t size) {
        std::cout << "Factory creating unique resource..." << std::endl;
        // name is moved into the constructor
        return std::make_unique<MovableResource>(std::move(name), size);
    }

    // Batch creation with move semantics
    static std::vector<MovableResource> createBatch(const std::vector<std::string>& names, size_t size) {
        std::vector<MovableResource> batch;
        batch.reserve(names.size()); // Prevent reallocations

        for (const auto& name : names) {
            batch.emplace_back(name, size); // Construct in-place
        }

        return batch; // RVO or move constructor for return
    }
};

// Perfect forwarding example
template<typename T, typename... Args>
std::unique_ptr<T> make_unique_perfect(Args&&... args) {
    std::cout << "Perfect forwarding to constructor..." << std::endl;
    return std::unique_ptr<T>(new T(std::forward<Args>(args)...));
}

void testBasicMoveSemantics() {
    std::cout << "=== Testing Basic Move Semantics ===" << std::endl;

    // Create resource
    MovableResource resource1("TestResource", 1000);
    resource1.printData();

    // Copy vs Move demonstration
    {
        std::cout << "\\n--- Copy vs Move ---" << std::endl;

        // Copy constructor
        MovableResource copied_resource = resource1;
        copied_resource.printData();

        // Move constructor
        MovableResource moved_resource = std::move(resource1);
        moved_resource.printData();

        std::cout << "Original resource after move: ";
        resource1.printData(); // Should be invalid
    }

    std::cout << "\\nCreating new resource for assignment test..." << std::endl;
    MovableResource resource2("AssignmentTest", 500);
    MovableResource resource3("AssignmentTarget", 100);

    // Assignment operators
    std::cout << "\\n--- Assignment Operators ---" << std::endl;

    // Copy assignment
    MovableResource copy_assigned("Empty", 0);
    copy_assigned = resource2;
    copy_assigned.printData();

    // Move assignment
    MovableResource move_assigned("Empty", 0);
    move_assigned = std::move(resource3);
    move_assigned.printData();

    std::cout << "Resource3 after move assignment: ";
    resource3.printData(); // Should be invalid
}

void testContainerMoveSemantics() {
    std::cout << "=== Testing Container Move Semantics ===" << std::endl;

    ResourceContainer container;

    // Test different ways of adding resources
    MovableResource resource1("ContainerTest1", 100);

    // Add by copy (expensive)
    container.addResourceByCopy(resource1);

    // Add by move (efficient)
    MovableResource resource2("ContainerTest2", 200);
    container.addResourceByMove(std::move(resource2));

    // Emplace (most efficient - no temporary objects)
    container.emplaceResource("ContainerTest3", 300);

    container.printAllResources();

    // Remove resource (move semantics)
    std::cout << "\\nRemoving resource at index 1..." << std::endl;
    try {
        MovableResource removed = container.removeResource(1);
        std::cout << "Removed resource: ";
        removed.printData();
    }
    catch (const std::exception& e) {
        std::cout << "Error: " << e.what() << std::endl;
    }

    container.printAllResources();
}

void testFactoryMoveSemantics() {
    std::cout << "=== Testing Factory Move Semantics ===" << std::endl;

    // Factory returning by value
    MovableResource factory_resource = ResourceFactory::createResource("FactoryResource", 400);
    factory_resource.printData();

    // Factory with unique_ptr
    auto unique_resource = ResourceFactory::createUniqueResource("UniqueFactoryResource", 500);
    unique_resource->printData();

    // Batch creation
    std::vector<std::string> names = {"Batch1", "Batch2", "Batch3"};
    auto batch = ResourceFactory::createBatch(names, 150);

    std::cout << "Created batch of " << batch.size() << " resources:" << std::endl;
    for (size_t i = 0; i < batch.size(); ++i) {
        std::cout << "  ";
        batch[i].printData();
    }
}

void testPerfectForwarding() {
    std::cout << "=== Testing Perfect Forwarding ===" << std::endl;

    // Test perfect forwarding with different argument types
    std::string name = "PerfectResource";

    // Forward lvalue reference
    auto resource1 = make_unique_perfect<MovableResource>(name, 600);
    resource1->printData();

    // Forward rvalue reference
    auto resource2 = make_unique_perfect<MovableResource>(std::string("TempString"), 700);
    resource2->printData();

    // Forward with literal
    auto resource3 = make_unique_perfect<MovableResource>("LiteralString", 800);
    resource3->printData();
}

void testSTLMoveSemantics() {
    std::cout << "=== Testing STL Move Semantics ===" << std::endl;

    std::vector<MovableResource> vec;
    vec.reserve(5); // Prevent reallocations

    // Push back with move
    for (int i = 0; i < 3; ++i) {
        std::string name = "STLResource" + std::to_string(i);
        vec.emplace_back(name, 100 + i * 50); // Direct construction
    }

    std::cout << "Vector contents:" << std::endl;
    for (size_t i = 0; i < vec.size(); ++i) {
        std::cout << "  [" << i << "] ";
        vec[i].printData();
    }

    // Move from vector
    std::cout << "\\nMoving resource out of vector..." << std::endl;
    MovableResource moved_from_vec = std::move(vec[1]);
    moved_from_vec.printData();

    std::cout << "Vector after move:" << std::endl;
    for (size_t i = 0; i < vec.size(); ++i) {
        std::cout << "  [" << i << "] ";
        vec[i].printData();
    }
}

void demonstrateMoveSemantics() {
    testBasicMoveSemantics();
    testContainerMoveSemantics();
    testFactoryMoveSemantics();
    testPerfectForwarding();
    testSTLMoveSemantics();
}
""",
    )

    run_updater(cpp_memory_project, mock_ingestor)

    project_name = cpp_memory_project.name

    expected_classes = [
        f"{project_name}.move_semantics.MovableResource",
        f"{project_name}.move_semantics.ResourceContainer",
        f"{project_name}.move_semantics.ResourceFactory",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_cpp_memory_management_comprehensive(
    cpp_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all memory management patterns create proper relationships."""
    test_file = cpp_memory_project / "comprehensive_memory.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive memory management combining smart pointers, RAII, and move semantics
#include <iostream>
#include <memory>
#include <vector>

class ComprehensiveMemoryDemo {
private:
    std::vector<std::unique_ptr<int>> managed_data_;
    std::shared_ptr<std::string> shared_resource_;

public:
    ComprehensiveMemoryDemo() {
        shared_resource_ = std::make_shared<std::string>("Shared Resource");
    }

    void demonstrateMemoryManagement() {
        std::cout << "=== Comprehensive Memory Management Demo ===" << std::endl;

        // Smart pointer management
        for (int i = 0; i < 5; ++i) {
            managed_data_.push_back(std::make_unique<int>(i * 10));
        }

        // Process managed data
        processData();

        // Move semantics with smart pointers
        auto moved_data = std::move(managed_data_);
        std::cout << "Data moved, original size: " << managed_data_.size() << std::endl;
        std::cout << "Moved data size: " << moved_data.size() << std::endl;
    }

private:
    void processData() {
        std::cout << "Processing " << managed_data_.size() << " managed items:" << std::endl;
        for (const auto& item : managed_data_) {
            std::cout << "  Value: " << *item << std::endl;
        }
    }
};

void demonstrateComprehensiveMemoryManagement() {
    ComprehensiveMemoryDemo demo;
    demo.demonstrateMemoryManagement();
}
""",
    )

    run_updater(cpp_memory_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_memory" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive memory management calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
