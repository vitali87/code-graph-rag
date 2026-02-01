from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_stl_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with STL usage."""
    project_path = temp_repo / "cpp_stl_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_stl_containers(
    cpp_stl_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test STL containers including vector, map, set, deque, list."""
    test_file = cpp_stl_project / "stl_containers.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <map>
#include <unordered_map>
#include <set>
#include <unordered_set>
#include <deque>
#include <list>
#include <forward_list>
#include <stack>
#include <queue>
#include <priority_queue>
#include <array>
#include <string>
#include <algorithm>

// STL Container demonstration class
class STLContainerDemo {
private:
    std::vector<int> numbers_;
    std::map<std::string, int> scores_;
    std::set<std::string> unique_names_;

public:
    STLContainerDemo() {
        // Initialize containers
        numbers_ = {1, 2, 3, 4, 5};
        scores_["Alice"] = 95;
        scores_["Bob"] = 87;
        scores_["Charlie"] = 92;

        unique_names_.insert("Alice");
        unique_names_.insert("Bob");
        unique_names_.insert("Charlie");
    }

    void demonstrateSequenceContainers() {
        std::cout << "=== Sequence Containers ===" << std::endl;

        // Vector operations
        std::vector<int> vec = {10, 20, 30, 40, 50};
        vec.push_back(60);
        vec.insert(vec.begin() + 2, 25);

        std::cout << "Vector contents: ";
        for (const auto& val : vec) {
            std::cout << val << " ";
        }
        std::cout << std::endl;

        // Deque operations
        std::deque<std::string> deq = {"first", "second", "third"};
        deq.push_front("zero");
        deq.push_back("fourth");

        std::cout << "Deque contents: ";
        for (const auto& str : deq) {
            std::cout << str << " ";
        }
        std::cout << std::endl;

        // List operations
        std::list<double> lst = {1.1, 2.2, 3.3};
        lst.push_front(0.0);
        lst.push_back(4.4);
        lst.sort(); // List has its own sort method

        std::cout << "List contents: ";
        for (const auto& val : lst) {
            std::cout << val << " ";
        }
        std::cout << std::endl;

        // Array operations (C++11)
        std::array<int, 5> arr = {100, 200, 300, 400, 500};
        std::sort(arr.begin(), arr.end(), std::greater<int>());

        std::cout << "Array contents (sorted desc): ";
        for (const auto& val : arr) {
            std::cout << val << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateAssociativeContainers() {
        std::cout << "=== Associative Containers ===" << std::endl;

        // Map operations
        std::map<std::string, int> grades;
        grades["Mathematics"] = 95;
        grades["Physics"] = 88;
        grades["Chemistry"] = 92;
        grades["Biology"] = 90;

        std::cout << "Subject grades:" << std::endl;
        for (const auto& [subject, grade] : grades) {
            std::cout << "  " << subject << ": " << grade << std::endl;
        }

        // Multimap for duplicate keys
        std::multimap<int, std::string> score_to_student;
        score_to_student.insert({95, "Alice"});
        score_to_student.insert({87, "Bob"});
        score_to_student.insert({95, "Diana"});  // Duplicate score
        score_to_student.insert({92, "Charlie"});

        std::cout << "Students by score:" << std::endl;
        for (const auto& [score, student] : score_to_student) {
            std::cout << "  " << student << ": " << score << std::endl;
        }

        // Set operations
        std::set<int> unique_scores = {95, 87, 92, 95, 88, 90};  // Duplicates ignored
        std::cout << "Unique scores: ";
        for (const auto& score : unique_scores) {
            std::cout << score << " ";
        }
        std::cout << std::endl;

        // Set operations: union, intersection
        std::set<int> set1 = {1, 2, 3, 4, 5};
        std::set<int> set2 = {4, 5, 6, 7, 8};
        std::set<int> result;

        std::set_union(set1.begin(), set1.end(),
                      set2.begin(), set2.end(),
                      std::inserter(result, result.begin()));

        std::cout << "Union of sets: ";
        for (const auto& val : result) {
            std::cout << val << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateUnorderedContainers() {
        std::cout << "=== Unordered Containers ===" << std::endl;

        // Unordered map (hash table)
        std::unordered_map<std::string, int> word_count;
        std::vector<std::string> words = {"apple", "banana", "apple", "cherry", "banana", "apple"};

        for (const auto& word : words) {
            word_count[word]++;
        }

        std::cout << "Word frequencies:" << std::endl;
        for (const auto& [word, count] : word_count) {
            std::cout << "  " << word << ": " << count << std::endl;
        }

        // Unordered set
        std::unordered_set<std::string> unique_words(words.begin(), words.end());
        std::cout << "Unique words: ";
        for (const auto& word : unique_words) {
            std::cout << word << " ";
        }
        std::cout << std::endl;

        // Performance comparison hint
        std::cout << "Hash table bucket count: " << word_count.bucket_count() << std::endl;
        std::cout << "Load factor: " << word_count.load_factor() << std::endl;
    }

    void demonstrateContainerAdaptors() {
        std::cout << "=== Container Adaptors ===" << std::endl;

        // Stack (LIFO)
        std::stack<int> stk;
        for (int i = 1; i <= 5; ++i) {
            stk.push(i);
        }

        std::cout << "Stack contents (popped): ";
        while (!stk.empty()) {
            std::cout << stk.top() << " ";
            stk.pop();
        }
        std::cout << std::endl;

        // Queue (FIFO)
        std::queue<std::string> que;
        que.push("first");
        que.push("second");
        que.push("third");

        std::cout << "Queue contents (dequeued): ";
        while (!que.empty()) {
            std::cout << que.front() << " ";
            que.pop();
        }
        std::cout << std::endl;

        // Priority queue (max heap by default)
        std::priority_queue<int> pq;
        std::vector<int> values = {30, 10, 50, 20, 40};

        for (int val : values) {
            pq.push(val);
        }

        std::cout << "Priority queue (max heap): ";
        while (!pq.empty()) {
            std::cout << pq.top() << " ";
            pq.pop();
        }
        std::cout << std::endl;

        // Priority queue with custom comparator (min heap)
        std::priority_queue<int, std::vector<int>, std::greater<int>> min_pq;
        for (int val : values) {
            min_pq.push(val);
        }

        std::cout << "Priority queue (min heap): ";
        while (!min_pq.empty()) {
            std::cout << min_pq.top() << " ";
            min_pq.pop();
        }
        std::cout << std::endl;
    }

    void demonstrateAdvancedOperations() {
        std::cout << "=== Advanced Container Operations ===" << std::endl;

        // Container capacity and memory management
        std::vector<int> vec;
        std::cout << "Vector capacity progression:" << std::endl;
        for (int i = 0; i < 10; ++i) {
            vec.push_back(i);
            std::cout << "  Size: " << vec.size() << ", Capacity: " << vec.capacity() << std::endl;
        }

        // Reserve memory to avoid reallocations
        std::vector<int> reserved_vec;
        reserved_vec.reserve(100);
        std::cout << "Reserved vector capacity: " << reserved_vec.capacity() << std::endl;

        // Container swapping
        std::vector<int> vec1 = {1, 2, 3};
        std::vector<int> vec2 = {4, 5, 6, 7, 8};

        std::cout << "Before swap - vec1 size: " << vec1.size() << ", vec2 size: " << vec2.size() << std::endl;
        vec1.swap(vec2);
        std::cout << "After swap - vec1 size: " << vec1.size() << ", vec2 size: " << vec2.size() << std::endl;

        // Container comparison
        std::vector<int> v1 = {1, 2, 3};
        std::vector<int> v2 = {1, 2, 3};
        std::vector<int> v3 = {1, 2, 4};

        std::cout << "v1 == v2: " << std::boolalpha << (v1 == v2) << std::endl;
        std::cout << "v1 < v3: " << std::boolalpha << (v1 < v3) << std::endl;
    }
};

void testSTLContainers() {
    STLContainerDemo demo;
    demo.demonstrateSequenceContainers();
    demo.demonstrateAssociativeContainers();
    demo.demonstrateUnorderedContainers();
    demo.demonstrateContainerAdaptors();
    demo.demonstrateAdvancedOperations();
}

// Graph-like structure using STL containers
class GraphStructure {
private:
    std::unordered_map<int, std::vector<int>> adjacency_list_;
    std::unordered_map<int, std::string> node_labels_;

public:
    void addNode(int id, const std::string& label) {
        node_labels_[id] = label;
        if (adjacency_list_.find(id) == adjacency_list_.end()) {
            adjacency_list_[id] = std::vector<int>();
        }
    }

    void addEdge(int from, int to) {
        adjacency_list_[from].push_back(to);
    }

    void printGraph() const {
        std::cout << "Graph structure:" << std::endl;
        for (const auto& [node, neighbors] : adjacency_list_) {
            std::cout << "Node " << node << " (" << node_labels_.at(node) << "): ";
            for (int neighbor : neighbors) {
                std::cout << neighbor << " ";
            }
            std::cout << std::endl;
        }
    }

    std::vector<int> getNeighbors(int node) const {
        auto it = adjacency_list_.find(node);
        return (it != adjacency_list_.end()) ? it->second : std::vector<int>();
    }

    size_t getNodeCount() const { return node_labels_.size(); }
    size_t getEdgeCount() const {
        size_t count = 0;
        for (const auto& [node, neighbors] : adjacency_list_) {
            count += neighbors.size();
        }
        return count;
    }
};

void testGraphStructure() {
    std::cout << "=== Graph Structure Demo ===" << std::endl;

    GraphStructure graph;

    // Add nodes
    graph.addNode(1, "Function A");
    graph.addNode(2, "Function B");
    graph.addNode(3, "Function C");
    graph.addNode(4, "Class X");
    graph.addNode(5, "Class Y");

    // Add edges (function calls, inheritance, etc.)
    graph.addEdge(1, 2);  // A calls B
    graph.addEdge(1, 3);  // A calls C
    graph.addEdge(2, 4);  // B uses X
    graph.addEdge(3, 4);  // C uses X
    graph.addEdge(5, 4);  // Y inherits from X

    graph.printGraph();

    std::cout << "Graph statistics:" << std::endl;
    std::cout << "  Nodes: " << graph.getNodeCount() << std::endl;
    std::cout << "  Edges: " << graph.getEdgeCount() << std::endl;

    // Query neighbors
    auto neighbors = graph.getNeighbors(1);
    std::cout << "Node 1 neighbors: ";
    for (int neighbor : neighbors) {
        std::cout << neighbor << " ";
    }
    std::cout << std::endl;
}

void demonstrateSTLContainers() {
    testSTLContainers();
    testGraphStructure();
}
""",
    )

    run_updater(cpp_stl_project, mock_ingestor)

    project_name = cpp_stl_project.name

    expected_classes = [
        f"{project_name}.stl_containers.STLContainerDemo",
        f"{project_name}.stl_containers.GraphStructure",
    ]

    expected_functions = [
        f"{project_name}.stl_containers.testSTLContainers",
        f"{project_name}.stl_containers.testGraphStructure",
        f"{project_name}.stl_containers.demonstrateSTLContainers",
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


def test_stl_algorithms(
    cpp_stl_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test STL algorithms including sorting, searching, and transforming."""
    test_file = cpp_stl_project / "stl_algorithms.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <algorithm>
#include <numeric>
#include <functional>
#include <iterator>
#include <string>
#include <random>

// STL Algorithms demonstration
class STLAlgorithmsDemo {
private:
    std::vector<int> numbers_;
    std::vector<std::string> words_;

public:
    STLAlgorithmsDemo() {
        numbers_ = {64, 34, 25, 12, 22, 11, 90, 5, 77, 30};
        words_ = {"apple", "banana", "cherry", "date", "elderberry", "fig", "grape"};
    }

    void demonstrateSortingAlgorithms() {
        std::cout << "=== Sorting Algorithms ===" << std::endl;

        // Copy original data
        auto nums = numbers_;

        std::cout << "Original: ";
        printContainer(nums);

        // std::sort (default ascending)
        std::sort(nums.begin(), nums.end());
        std::cout << "std::sort (asc): ";
        printContainer(nums);

        // std::sort with comparator (descending)
        std::sort(nums.begin(), nums.end(), std::greater<int>());
        std::cout << "std::sort (desc): ";
        printContainer(nums);

        // std::partial_sort (sort first n elements)
        nums = numbers_;  // Reset
        std::partial_sort(nums.begin(), nums.begin() + 5, nums.end());
        std::cout << "partial_sort (first 5): ";
        printContainer(nums);

        // std::nth_element (find nth element in sorted order)
        nums = numbers_;  // Reset
        std::nth_element(nums.begin(), nums.begin() + 4, nums.end());
        std::cout << "nth_element (5th): " << nums[4] << " at position 4" << std::endl;

        // std::stable_sort (maintains relative order of equal elements)
        std::vector<std::pair<int, char>> pairs = {{3, 'a'}, {1, 'b'}, {3, 'c'}, {2, 'd'}};
        std::stable_sort(pairs.begin(), pairs.end(),
                        [](const auto& a, const auto& b) { return a.first < b.first; });

        std::cout << "stable_sort pairs: ";
        for (const auto& [num, letter] : pairs) {
            std::cout << "(" << num << "," << letter << ") ";
        }
        std::cout << std::endl;
    }

    void demonstrateSearchingAlgorithms() {
        std::cout << "=== Searching Algorithms ===" << std::endl;

        auto sorted_nums = numbers_;
        std::sort(sorted_nums.begin(), sorted_nums.end());

        std::cout << "Sorted array: ";
        printContainer(sorted_nums);

        // std::binary_search
        int target = 25;
        bool found = std::binary_search(sorted_nums.begin(), sorted_nums.end(), target);
        std::cout << "binary_search for " << target << ": " << std::boolalpha << found << std::endl;

        // std::lower_bound and std::upper_bound
        auto lower = std::lower_bound(sorted_nums.begin(), sorted_nums.end(), target);
        auto upper = std::upper_bound(sorted_nums.begin(), sorted_nums.end(), target);

        if (lower != sorted_nums.end()) {
            std::cout << "lower_bound for " << target << ": position "
                      << std::distance(sorted_nums.begin(), lower) << std::endl;
        }

        // std::find and std::find_if
        auto it = std::find(numbers_.begin(), numbers_.end(), 77);
        if (it != numbers_.end()) {
            std::cout << "find 77: found at position "
                      << std::distance(numbers_.begin(), it) << std::endl;
        }

        auto even_it = std::find_if(numbers_.begin(), numbers_.end(),
                                   [](int n) { return n % 2 == 0; });
        if (even_it != numbers_.end()) {
            std::cout << "find_if (first even): " << *even_it << std::endl;
        }

        // std::count and std::count_if
        int count_above_30 = std::count_if(numbers_.begin(), numbers_.end(),
                                          [](int n) { return n > 30; });
        std::cout << "count_if (> 30): " << count_above_30 << std::endl;
    }

    void demonstrateTransformingAlgorithms() {
        std::cout << "=== Transforming Algorithms ===" << std::endl;

        // std::transform (unary operation)
        std::vector<int> squares(numbers_.size());
        std::transform(numbers_.begin(), numbers_.end(), squares.begin(),
                      [](int n) { return n * n; });

        std::cout << "Original: ";
        printContainer(numbers_);
        std::cout << "Squares: ";
        printContainer(squares);

        // std::transform (binary operation)
        std::vector<int> sums(numbers_.size());
        std::transform(numbers_.begin(), numbers_.end(), squares.begin(), sums.begin(),
                      [](int a, int b) { return a + b; });

        std::cout << "Sums (orig + square): ";
        printContainer(sums);

        // std::for_each
        std::cout << "for_each (multiply by 2): ";
        auto doubled = numbers_;
        std::for_each(doubled.begin(), doubled.end(), [](int& n) { n *= 2; });
        printContainer(doubled);

        // std::generate
        std::vector<int> random_nums(10);
        std::random_device rd;
        std::mt19937 gen(rd());
        std::uniform_int_distribution<> dis(1, 100);

        std::generate(random_nums.begin(), random_nums.end(),
                     [&]() { return dis(gen); });

        std::cout << "Generated random: ";
        printContainer(random_nums);
    }

    void demonstrateNumericAlgorithms() {
        std::cout << "=== Numeric Algorithms ===" << std::endl;

        // std::accumulate
        int sum = std::accumulate(numbers_.begin(), numbers_.end(), 0);
        std::cout << "Sum (accumulate): " << sum << std::endl;

        // std::accumulate with custom operation
        int product = std::accumulate(numbers_.begin(), numbers_.begin() + 5, 1,
                                     [](int a, int b) { return a * b; });
        std::cout << "Product of first 5: " << product << std::endl;

        // std::inner_product
        std::vector<int> weights = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
        int weighted_sum = std::inner_product(numbers_.begin(), numbers_.end(),
                                             weights.begin(), 0);
        std::cout << "Weighted sum: " << weighted_sum << std::endl;

        // std::partial_sum
        std::vector<int> cumulative_sums(numbers_.size());
        std::partial_sum(numbers_.begin(), numbers_.end(), cumulative_sums.begin());

        std::cout << "Original: ";
        printContainer(numbers_);
        std::cout << "Cumulative sums: ";
        printContainer(cumulative_sums);

        // std::adjacent_difference
        std::vector<int> differences(numbers_.size());
        std::adjacent_difference(numbers_.begin(), numbers_.end(), differences.begin());

        std::cout << "Adjacent differences: ";
        printContainer(differences);

        // std::iota (fill with sequential values)
        std::vector<int> sequence(10);
        std::iota(sequence.begin(), sequence.end(), 1);

        std::cout << "iota sequence: ";
        printContainer(sequence);
    }

    void demonstrateSetOperations() {
        std::cout << "=== Set Operations ===" << std::endl;

        std::vector<int> set1 = {1, 2, 3, 4, 5, 6};
        std::vector<int> set2 = {4, 5, 6, 7, 8, 9};

        // Ensure sets are sorted
        std::sort(set1.begin(), set1.end());
        std::sort(set2.begin(), set2.end());

        std::cout << "Set 1: ";
        printContainer(set1);
        std::cout << "Set 2: ";
        printContainer(set2);

        // std::set_union
        std::vector<int> union_result;
        std::set_union(set1.begin(), set1.end(),
                      set2.begin(), set2.end(),
                      std::back_inserter(union_result));
        std::cout << "Union: ";
        printContainer(union_result);

        // std::set_intersection
        std::vector<int> intersection_result;
        std::set_intersection(set1.begin(), set1.end(),
                             set2.begin(), set2.end(),
                             std::back_inserter(intersection_result));
        std::cout << "Intersection: ";
        printContainer(intersection_result);

        // std::set_difference
        std::vector<int> difference_result;
        std::set_difference(set1.begin(), set1.end(),
                           set2.begin(), set2.end(),
                           std::back_inserter(difference_result));
        std::cout << "Difference (1-2): ";
        printContainer(difference_result);

        // std::set_symmetric_difference
        std::vector<int> symmetric_diff;
        std::set_symmetric_difference(set1.begin(), set1.end(),
                                     set2.begin(), set2.end(),
                                     std::back_inserter(symmetric_diff));
        std::cout << "Symmetric difference: ";
        printContainer(symmetric_diff);
    }

    void demonstratePermutationAlgorithms() {
        std::cout << "=== Permutation Algorithms ===" << std::endl;

        std::vector<int> small_set = {1, 2, 3, 4};
        std::cout << "Original: ";
        printContainer(small_set);

        // std::next_permutation
        std::cout << "All permutations:" << std::endl;
        int count = 0;
        do {
            std::cout << "  ";
            printContainer(small_set);
            ++count;
        } while (std::next_permutation(small_set.begin(), small_set.end()) && count < 10);

        // std::prev_permutation
        std::vector<int> reverse_set = {4, 3, 2, 1};
        std::cout << "Reverse permutations (first 5):" << std::endl;
        count = 0;
        do {
            std::cout << "  ";
            printContainer(reverse_set);
            ++count;
        } while (std::prev_permutation(reverse_set.begin(), reverse_set.end()) && count < 5);

        // std::random_shuffle (deprecated) / std::shuffle
        auto shuffled = numbers_;
        std::random_device rd;
        std::mt19937 g(rd());
        std::shuffle(shuffled.begin(), shuffled.end(), g);

        std::cout << "Original: ";
        printContainer(numbers_);
        std::cout << "Shuffled: ";
        printContainer(shuffled);
    }

private:
    template<typename Container>
    void printContainer(const Container& container) {
        for (const auto& item : container) {
            std::cout << item << " ";
        }
        std::cout << std::endl;
    }
};

void testSTLAlgorithms() {
    STLAlgorithmsDemo demo;
    demo.demonstrateSortingAlgorithms();
    demo.demonstrateSearchingAlgorithms();
    demo.demonstrateTransformingAlgorithms();
    demo.demonstrateNumericAlgorithms();
    demo.demonstrateSetOperations();
    demo.demonstratePermutationAlgorithms();
}

void demonstrateSTLAlgorithms() {
    testSTLAlgorithms();
}
""",
    )

    run_updater(cpp_stl_project, mock_ingestor)

    project_name = cpp_stl_project.name

    expected_classes = [
        f"{project_name}.stl_algorithms.STLAlgorithmsDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 STL algorithms class, found {len(found_classes)}: {found_classes}"
    )


def test_stl_iterators_functors(
    cpp_stl_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test STL iterators and function objects."""
    test_file = cpp_stl_project / "stl_iterators_functors.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <vector>
#include <list>
#include <iterator>
#include <algorithm>
#include <functional>
#include <string>

// Custom iterator example
template<typename T>
class SimpleVector {
private:
    T* data_;
    size_t size_;
    size_t capacity_;

public:
    // Iterator class
    class Iterator {
    private:
        T* ptr_;

    public:
        using iterator_category = std::random_access_iterator_tag;
        using value_type = T;
        using difference_type = std::ptrdiff_t;
        using pointer = T*;
        using reference = T&;

        Iterator(T* ptr) : ptr_(ptr) {}

        reference operator*() const { return *ptr_; }
        pointer operator->() const { return ptr_; }

        Iterator& operator++() { ++ptr_; return *this; }
        Iterator operator++(int) { Iterator temp = *this; ++ptr_; return temp; }

        Iterator& operator--() { --ptr_; return *this; }
        Iterator operator--(int) { Iterator temp = *this; --ptr_; return temp; }

        Iterator operator+(difference_type n) const { return Iterator(ptr_ + n); }
        Iterator operator-(difference_type n) const { return Iterator(ptr_ - n); }

        difference_type operator-(const Iterator& other) const { return ptr_ - other.ptr_; }

        bool operator==(const Iterator& other) const { return ptr_ == other.ptr_; }
        bool operator!=(const Iterator& other) const { return ptr_ != other.ptr_; }
        bool operator<(const Iterator& other) const { return ptr_ < other.ptr_; }
    };

    SimpleVector() : data_(nullptr), size_(0), capacity_(0) {}

    SimpleVector(size_t capacity) : size_(0), capacity_(capacity) {
        data_ = new T[capacity_];
    }

    ~SimpleVector() { delete[] data_; }

    void push_back(const T& value) {
        if (size_ < capacity_) {
            data_[size_++] = value;
        }
    }

    Iterator begin() { return Iterator(data_); }
    Iterator end() { return Iterator(data_ + size_); }

    size_t size() const { return size_; }
};

// Custom function objects
class Multiplier {
private:
    int factor_;

public:
    Multiplier(int factor) : factor_(factor) {}

    int operator()(int value) const {
        return value * factor_;
    }
};

class StringLengthComparator {
public:
    bool operator()(const std::string& a, const std::string& b) const {
        return a.length() < b.length();
    }
};

// Predicate functors
struct IsEven {
    bool operator()(int n) const { return n % 2 == 0; }
};

struct IsPositive {
    bool operator()(int n) const { return n > 0; }
};

class STLIteratorsFunctorsDemo {
private:
    std::vector<int> numbers_;
    std::vector<std::string> words_;

public:
    STLIteratorsFunctorsDemo() {
        numbers_ = {1, -2, 3, -4, 5, 6, -7, 8, 9, -10};
        words_ = {"a", "hello", "world", "cpp", "programming", "iterators", "stl", "test"};
    }

    void demonstrateIteratorTypes() {
        std::cout << "=== Iterator Types ===" << std::endl;

        // Input iterator (read-only, forward)
        std::vector<int> vec = {1, 2, 3, 4, 5};
        std::cout << "Forward iteration: ";
        for (auto it = vec.begin(); it != vec.end(); ++it) {
            std::cout << *it << " ";
        }
        std::cout << std::endl;

        // Bidirectional iterator
        std::list<int> lst = {10, 20, 30, 40, 50};
        std::cout << "Reverse iteration: ";
        for (auto it = lst.rbegin(); it != lst.rend(); ++it) {
            std::cout << *it << " ";
        }
        std::cout << std::endl;

        // Random access iterator
        std::cout << "Random access: ";
        auto random_it = vec.begin();
        std::cout << "vec[0] = " << *random_it << ", ";
        std::cout << "vec[3] = " << *(random_it + 3) << ", ";
        std::cout << "vec[4] = " << vec[4] << std::endl;

        // Distance between iterators
        auto distance = std::distance(vec.begin(), vec.end());
        std::cout << "Distance from begin to end: " << distance << std::endl;
    }

    void demonstrateIteratorAdaptors() {
        std::cout << "=== Iterator Adaptors ===" << std::endl;

        // Back insert iterator
        std::vector<int> target;
        std::copy(numbers_.begin(), numbers_.begin() + 5, std::back_inserter(target));

        std::cout << "Back inserter result: ";
        for (int n : target) std::cout << n << " ";
        std::cout << std::endl;

        // Front insert iterator (with deque)
        std::deque<int> deq;
        std::copy(numbers_.begin(), numbers_.begin() + 5, std::front_inserter(deq));

        std::cout << "Front inserter result: ";
        for (int n : deq) std::cout << n << " ";
        std::cout << std::endl;

        // Insert iterator
        std::vector<int> vec = {100, 200};
        auto insert_pos = vec.begin() + 1;
        std::copy(numbers_.begin(), numbers_.begin() + 3, std::inserter(vec, insert_pos));

        std::cout << "Insert iterator result: ";
        for (int n : vec) std::cout << n << " ";
        std::cout << std::endl;

        // Reverse iterator
        std::cout << "Reverse iterator: ";
        for (auto rit = numbers_.rbegin(); rit != numbers_.rbegin() + 5; ++rit) {
            std::cout << *rit << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateStreamIterators() {
        std::cout << "=== Stream Iterators ===" << std::endl;

        // Ostream iterator
        std::cout << "Numbers with ostream_iterator: ";
        std::copy(numbers_.begin(), numbers_.begin() + 5,
                 std::ostream_iterator<int>(std::cout, " "));
        std::cout << std::endl;

        // Using ostream iterator with transform
        std::cout << "Squared numbers: ";
        std::transform(numbers_.begin(), numbers_.begin() + 5,
                      std::ostream_iterator<int>(std::cout, " "),
                      [](int n) { return n * n; });
        std::cout << std::endl;
    }

    void demonstrateCustomIterator() {
        std::cout << "=== Custom Iterator ===" << std::endl;

        SimpleVector<int> custom_vec(10);
        for (int i = 1; i <= 5; ++i) {
            custom_vec.push_back(i * 10);
        }

        std::cout << "Custom vector contents: ";
        for (auto it = custom_vec.begin(); it != custom_vec.end(); ++it) {
            std::cout << *it << " ";
        }
        std::cout << std::endl;

        // Use STL algorithms with custom iterator
        std::cout << "Custom vector sorted: ";
        std::sort(custom_vec.begin(), custom_vec.end());
        for (const auto& value : custom_vec) {
            std::cout << value << " ";
        }
        std::cout << std::endl;
    }

    void demonstrateStandardFunctors() {
        std::cout << "=== Standard Function Objects ===" << std::endl;

        auto nums = numbers_;

        // Arithmetic functors
        std::transform(nums.begin(), nums.end(), nums.begin(),
                      std::bind(std::multiplies<int>(), std::placeholders::_1, 2));
        std::cout << "Multiplied by 2: ";
        for (int n : nums) std::cout << n << " ";
        std::cout << std::endl;

        // Comparison functors
        nums = numbers_;
        std::sort(nums.begin(), nums.end(), std::greater<int>());
        std::cout << "Sorted descending: ";
        for (int n : nums) std::cout << n << " ";
        std::cout << std::endl;

        // Logical functors
        std::vector<bool> even_flags(numbers_.size());
        std::transform(numbers_.begin(), numbers_.end(), even_flags.begin(),
                      [](int n) { return n % 2 == 0; });

        std::cout << "Even flags: ";
        for (bool flag : even_flags) std::cout << std::boolalpha << flag << " ";
        std::cout << std::endl;
    }

    void demonstrateCustomFunctors() {
        std::cout << "=== Custom Function Objects ===" << std::endl;

        // Custom multiplier functor
        Multiplier times3(3);
        std::vector<int> multiplied(numbers_.size());
        std::transform(numbers_.begin(), numbers_.end(), multiplied.begin(), times3);

        std::cout << "Original: ";
        for (int n : numbers_) std::cout << n << " ";
        std::cout << std::endl;

        std::cout << "Multiplied by 3: ";
        for (int n : multiplied) std::cout << n << " ";
        std::cout << std::endl;

        // Custom string comparator
        auto words_copy = words_;
        std::sort(words_copy.begin(), words_copy.end(), StringLengthComparator());

        std::cout << "Words sorted by length: ";
        for (const auto& word : words_copy) std::cout << word << " ";
        std::cout << std::endl;

        // Predicate functors
        int even_count = std::count_if(numbers_.begin(), numbers_.end(), IsEven());
        int positive_count = std::count_if(numbers_.begin(), numbers_.end(), IsPositive());

        std::cout << "Even numbers: " << even_count << std::endl;
        std::cout << "Positive numbers: " << positive_count << std::endl;
    }

    void demonstrateLambdasAndBind() {
        std::cout << "=== Lambdas and std::bind ===" << std::endl;

        // Lambda expressions
        auto square = [](int n) { return n * n; };
        auto is_greater_than = [](int threshold, int value) { return value > threshold; };

        std::vector<int> squares(numbers_.size());
        std::transform(numbers_.begin(), numbers_.end(), squares.begin(), square);

        std::cout << "Squares: ";
        for (int n : squares) std::cout << n << " ";
        std::cout << std::endl;

        // std::bind usage
        auto is_greater_than_5 = std::bind(is_greater_than, 5, std::placeholders::_1);
        int count_gt_5 = std::count_if(numbers_.begin(), numbers_.end(), is_greater_than_5);
        std::cout << "Numbers > 5: " << count_gt_5 << std::endl;

        // std::function
        std::function<bool(int)> predicate = [](int n) { return n < 0; };
        int negative_count = std::count_if(numbers_.begin(), numbers_.end(), predicate);
        std::cout << "Negative numbers: " << negative_count << std::endl;

        // Capturing lambda
        int threshold = 0;
        auto filter_by_threshold = [&threshold](int n) { return n > threshold; };

        std::vector<int> filtered;
        std::copy_if(numbers_.begin(), numbers_.end(), std::back_inserter(filtered),
                    filter_by_threshold);

        std::cout << "Filtered (> " << threshold << "): ";
        for (int n : filtered) std::cout << n << " ";
        std::cout << std::endl;
    }
};

void testSTLIteratorsAndFunctors() {
    STLIteratorsFunctorsDemo demo;
    demo.demonstrateIteratorTypes();
    demo.demonstrateIteratorAdaptors();
    demo.demonstrateStreamIterators();
    demo.demonstrateCustomIterator();
    demo.demonstrateStandardFunctors();
    demo.demonstrateCustomFunctors();
    demo.demonstrateLambdasAndBind();
}

void demonstrateSTLIteratorsAndFunctors() {
    testSTLIteratorsAndFunctors();
}
""",
    )

    run_updater(cpp_stl_project, mock_ingestor)

    project_name = cpp_stl_project.name

    expected_classes = [
        f"{project_name}.stl_iterators_functors.Multiplier",
        f"{project_name}.stl_iterators_functors.STLIteratorsFunctorsDemo",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 STL iterator/functor class, found {len(found_classes)}: {found_classes}"
    )


def test_cpp_stl_comprehensive(
    cpp_stl_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all STL features create proper relationships."""
    test_file = cpp_stl_project / "comprehensive_stl.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive STL usage combining containers, algorithms, and iterators
#include <iostream>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <numeric>
#include <functional>
#include <iterator>

class ComprehensiveSTLDemo {
private:
    std::map<std::string, std::vector<int>> data_groups_;
    std::set<std::string> processed_keys_;

public:
    ComprehensiveSTLDemo() {
        // Initialize with sample data
        data_groups_["group1"] = {5, 2, 8, 1, 9};
        data_groups_["group2"] = {3, 7, 4, 6, 0};
        data_groups_["group3"] = {15, 12, 18, 11, 19};
    }

    void processAllGroups() {
        std::cout << "=== Comprehensive STL Processing ===" << std::endl;

        // Use algorithms on each group
        for (auto& [key, values] : data_groups_) {
            processGroup(key, values);
            processed_keys_.insert(key);
        }

        // Summary statistics
        generateSummary();
    }

private:
    void processGroup(const std::string& key, std::vector<int>& values) {
        std::cout << "Processing " << key << ":" << std::endl;

        // Sort the values
        std::sort(values.begin(), values.end());
        std::cout << "  Sorted: ";
        std::copy(values.begin(), values.end(),
                 std::ostream_iterator<int>(std::cout, " "));
        std::cout << std::endl;

        // Calculate statistics using numeric algorithms
        int sum = std::accumulate(values.begin(), values.end(), 0);
        double average = static_cast<double>(sum) / values.size();

        auto [min_it, max_it] = std::minmax_element(values.begin(), values.end());

        std::cout << "  Sum: " << sum << ", Average: " << average << std::endl;
        std::cout << "  Min: " << *min_it << ", Max: " << *max_it << std::endl;

        // Transform values (square them)
        std::transform(values.begin(), values.end(), values.begin(),
                      [](int n) { return n * n; });

        std::cout << "  Squared: ";
        std::copy(values.begin(), values.end(),
                 std::ostream_iterator<int>(std::cout, " "));
        std::cout << std::endl;
    }

    void generateSummary() {
        std::cout << "=== Summary ===" << std::endl;

        std::cout << "Processed groups: ";
        std::copy(processed_keys_.begin(), processed_keys_.end(),
                 std::ostream_iterator<std::string>(std::cout, " "));
        std::cout << std::endl;

        // Combine all values into one vector
        std::vector<int> all_values;
        for (const auto& [key, values] : data_groups_) {
            std::copy(values.begin(), values.end(), std::back_inserter(all_values));
        }

        // Final statistics
        int total_sum = std::accumulate(all_values.begin(), all_values.end(), 0);
        std::cout << "Total sum of all squared values: " << total_sum << std::endl;
        std::cout << "Total processed elements: " << all_values.size() << std::endl;
    }
};

void demonstrateComprehensiveSTL() {
    ComprehensiveSTLDemo demo;
    demo.processAllGroups();
}
""",
    )

    run_updater(cpp_stl_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive_stl" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive STL calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
