from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_ranges_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with ranges and views patterns."""
    project_path = temp_repo / "cpp_ranges_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_ranges_algorithms(
    cpp_ranges_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic std::ranges algorithms and concepts."""
    test_file = cpp_ranges_project / "basic_ranges.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <ranges>
#include <algorithm>
#include <vector>
#include <list>
#include <string>
#include <iostream>
#include <numeric>
#include <functional>

// Range concepts demonstration
template<typename Range>
void analyzeRange(const Range& range) {
    std::cout << "Range Analysis:" << std::endl;

    if constexpr (std::ranges::range<Range>) {
        std::cout << "  - Is a range: true" << std::endl;
    }

    if constexpr (std::ranges::sized_range<Range>) {
        std::cout << "  - Is sized: true, size = " << std::ranges::size(range) << std::endl;
    }

    if constexpr (std::ranges::random_access_range<Range>) {
        std::cout << "  - Has random access: true" << std::endl;
    }

    if constexpr (std::ranges::bidirectional_range<Range>) {
        std::cout << "  - Is bidirectional: true" << std::endl;
    }

    if constexpr (std::ranges::forward_range<Range>) {
        std::cout << "  - Is forward: true" << std::endl;
    }

    if constexpr (std::ranges::input_range<Range>) {
        std::cout << "  - Is input: true" << std::endl;
    }

    if constexpr (std::ranges::output_range<Range, typename std::ranges::range_value_t<Range>>) {
        std::cout << "  - Is output: true" << std::endl;
    }

    if constexpr (std::ranges::contiguous_range<Range>) {
        std::cout << "  - Is contiguous: true" << std::endl;
    }

    if constexpr (std::ranges::common_range<Range>) {
        std::cout << "  - Is common: true" << std::endl;
    }

    if constexpr (std::ranges::viewable_range<Range>) {
        std::cout << "  - Is viewable: true" << std::endl;
    }
}

void testBasicRangesConcepts() {
    std::cout << "=== Testing Basic Ranges Concepts ===" << std::endl;

    std::vector<int> vec = {1, 2, 3, 4, 5};
    std::list<std::string> lst = {"hello", "world", "ranges"};
    int arr[] = {10, 20, 30, 40, 50};

    std::cout << "Vector analysis:" << std::endl;
    analyzeRange(vec);

    std::cout << "List analysis:" << std::endl;
    analyzeRange(lst);

    std::cout << "Array analysis:" << std::endl;
    analyzeRange(arr);
}

void testRangesAlgorithms() {
    std::cout << "=== Testing Ranges Algorithms ===" << std::endl;

    std::vector<int> numbers = {5, 2, 8, 1, 9, 3, 7, 4, 6};
    std::vector<std::string> words = {"apple", "banana", "cherry", "date", "elderberry"};

    // Sort using ranges
    std::ranges::sort(numbers);
    std::cout << "Sorted numbers: ";
    std::ranges::for_each(numbers, [](int n) { std::cout << n << " "; });
    std::cout << std::endl;

    // Sort with custom comparator
    std::ranges::sort(words, std::ranges::greater{});
    std::cout << "Reverse sorted words: ";
    std::ranges::for_each(words, [](const std::string& s) { std::cout << s << " "; });
    std::cout << std::endl;

    // Find operations
    auto it = std::ranges::find(numbers, 5);
    if (it != numbers.end()) {
        std::cout << "Found 5 at position: " << std::distance(numbers.begin(), it) << std::endl;
    }

    auto word_it = std::ranges::find_if(words, [](const std::string& s) {
        return s.length() > 6;
    });
    if (word_it != words.end()) {
        std::cout << "First word with length > 6: " << *word_it << std::endl;
    }

    // Count operations
    auto even_count = std::ranges::count_if(numbers, [](int n) { return n % 2 == 0; });
    std::cout << "Even numbers count: " << even_count << std::endl;

    // Transform operation
    std::vector<int> squared(numbers.size());
    std::ranges::transform(numbers, squared.begin(), [](int n) { return n * n; });
    std::cout << "Squared numbers: ";
    std::ranges::for_each(squared, [](int n) { std::cout << n << " "; });
    std::cout << std::endl;

    // Accumulate with ranges
    auto sum = std::accumulate(numbers.begin(), numbers.end(), 0);
    std::cout << "Sum of numbers: " << sum << std::endl;

    // All/any/none of operations
    bool all_positive = std::ranges::all_of(numbers, [](int n) { return n > 0; });
    bool any_large = std::ranges::any_of(numbers, [](int n) { return n > 8; });
    bool none_negative = std::ranges::none_of(numbers, [](int n) { return n < 0; });

    std::cout << "All positive: " << all_positive << ", Any large: " << any_large
              << ", None negative: " << none_negative << std::endl;
}

void testRangesProjections() {
    std::cout << "=== Testing Ranges Projections ===" << std::endl;

    struct Person {
        std::string name;
        int age;
        double height;

        Person(const std::string& n, int a, double h) : name(n), age(a), height(h) {}
    };

    std::vector<Person> people = {
        {"Alice", 30, 165.5},
        {"Bob", 25, 180.0},
        {"Charlie", 35, 175.2},
        {"Diana", 28, 160.8},
        {"Eve", 32, 170.0}
    };

    // Sort by age using projection
    std::ranges::sort(people, {}, &Person::age);
    std::cout << "Sorted by age: ";
    std::ranges::for_each(people, [](const Person& p) {
        std::cout << p.name << "(" << p.age << ") ";
    });
    std::cout << std::endl;

    // Find by name projection
    auto it = std::ranges::find(people, "Charlie", &Person::name);
    if (it != people.end()) {
        std::cout << "Found Charlie: age " << it->age << ", height " << it->height << std::endl;
    }

    // Min/max by height projection
    auto shortest = std::ranges::min_element(people, {}, &Person::height);
    auto tallest = std::ranges::max_element(people, {}, &Person::height);

    std::cout << "Shortest: " << shortest->name << " (" << shortest->height << "cm)" << std::endl;
    std::cout << "Tallest: " << tallest->name << " (" << tallest->height << "cm)" << std::endl;

    // Count adults (age >= 30) using projection
    auto adult_count = std::ranges::count_if(people, [](int age) { return age >= 30; }, &Person::age);
    std::cout << "Adults (age >= 30): " << adult_count << std::endl;
}

void testCustomRangeConcepts() {
    std::cout << "=== Testing Custom Range Concepts ===" << std::endl;

    // Custom range class
    class NumberSequence {
    private:
        int start_, end_, step_;

    public:
        NumberSequence(int start, int end, int step = 1)
            : start_(start), end_(end), step_(step) {}

        class iterator {
        private:
            int current_;
            int step_;

        public:
            using iterator_category = std::forward_iterator_tag;
            using value_type = int;
            using difference_type = std::ptrdiff_t;
            using pointer = int*;
            using reference = int&;

            iterator(int current, int step) : current_(current), step_(step) {}

            int operator*() const { return current_; }

            iterator& operator++() {
                current_ += step_;
                return *this;
            }

            iterator operator++(int) {
                iterator temp = *this;
                ++(*this);
                return temp;
            }

            bool operator==(const iterator& other) const {
                return current_ == other.current_;
            }

            bool operator!=(const iterator& other) const {
                return !(*this == other);
            }
        };

        iterator begin() const { return iterator(start_, step_); }
        iterator end() const {
            int actual_end = start_;
            while (actual_end < end_) {
                actual_end += step_;
            }
            return iterator(actual_end, step_);
        }

        std::size_t size() const {
            return static_cast<std::size_t>((end_ - start_ + step_ - 1) / step_);
        }

        bool empty() const { return start_ >= end_; }
    };

    NumberSequence seq(1, 10, 2);  // 1, 3, 5, 7, 9

    std::cout << "Custom range analysis:" << std::endl;
    analyzeRange(seq);

    std::cout << "Custom range contents: ";
    std::ranges::for_each(seq, [](int n) { std::cout << n << " "; });
    std::cout << std::endl;

    // Use standard algorithms with custom range
    auto it = std::ranges::find_if(seq, [](int n) { return n > 5; });
    if (it != seq.end()) {
        std::cout << "First element > 5: " << *it << std::endl;
    }

    auto count = std::ranges::count_if(seq, [](int n) { return n % 3 == 1; });
    std::cout << "Elements where n % 3 == 1: " << count << std::endl;
}

void demonstrateBasicRanges() {
    testBasicRangesConcepts();
    testRangesAlgorithms();
    testRangesProjections();
    testCustomRangeConcepts();
}
""",
    )

    run_updater(cpp_ranges_project, mock_ingestor)

    project_name = cpp_ranges_project.name

    expected_functions = [
        f"{project_name}.basic_ranges.testBasicRangesConcepts",
        f"{project_name}.basic_ranges.testRangesAlgorithms",
        f"{project_name}.basic_ranges.testRangesProjections",
        f"{project_name}.basic_ranges.testCustomRangeConcepts",
        f"{project_name}.basic_ranges.demonstrateBasicRanges",
    ]

    expected_classes = [
        f"{project_name}.basic_ranges.NumberSequence",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    created_classes = get_node_names(mock_ingestor, "Class")

    found_classes = [cls for cls in expected_classes if cls in created_classes]
    assert len(found_classes) >= 1, (
        f"Expected at least 1 ranges class, found {len(found_classes)}: {found_classes}"
    )


def test_views_and_adaptors(
    cpp_ranges_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test ranges views and view adaptors."""
    test_file = cpp_ranges_project / "views_adaptors.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <ranges>
#include <vector>
#include <string>
#include <iostream>
#include <functional>
#include <algorithm>

void testBasicViews() {
    std::cout << "=== Testing Basic Views ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

    // all_view (identity view)
    auto all_view = std::views::all(numbers);
    std::cout << "All view: ";
    for (auto n : all_view) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // filter_view
    auto even_numbers = numbers | std::views::filter([](int n) { return n % 2 == 0; });
    std::cout << "Even numbers: ";
    for (auto n : even_numbers) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // transform_view
    auto squared = numbers | std::views::transform([](int n) { return n * n; });
    std::cout << "Squared numbers: ";
    for (auto n : squared) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // take_view
    auto first_five = numbers | std::views::take(5);
    std::cout << "First 5 numbers: ";
    for (auto n : first_five) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // drop_view
    auto skip_three = numbers | std::views::drop(3);
    std::cout << "Skip first 3: ";
    for (auto n : skip_three) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // reverse_view
    auto reversed = numbers | std::views::reverse;
    std::cout << "Reversed: ";
    for (auto n : reversed) {
        std::cout << n << " ";
    }
    std::cout << std::endl;
}

void testAdvancedViews() {
    std::cout << "=== Testing Advanced Views ===" << std::endl;

    std::vector<std::string> words = {"hello", "world", "this", "is", "ranges", "testing"};

    // take_while_view
    auto short_words = words | std::views::take_while([](const std::string& s) {
        return s.length() <= 5;
    });
    std::cout << "Words with length <= 5: ";
    for (const auto& word : short_words) {
        std::cout << word << " ";
    }
    std::cout << std::endl;

    // drop_while_view
    auto after_long = words | std::views::drop_while([](const std::string& s) {
        return s.length() <= 4;
    });
    std::cout << "After first word with length > 4: ";
    for (const auto& word : after_long) {
        std::cout << word << " ";
    }
    std::cout << std::endl;

    // elements_view (for tuples/pairs)
    std::vector<std::pair<std::string, int>> name_age_pairs = {
        {"Alice", 30}, {"Bob", 25}, {"Charlie", 35}
    };

    auto names = name_age_pairs | std::views::elements<0>;
    std::cout << "Names: ";
    for (const auto& name : names) {
        std::cout << name << " ";
    }
    std::cout << std::endl;

    auto ages = name_age_pairs | std::views::elements<1>;
    std::cout << "Ages: ";
    for (auto age : ages) {
        std::cout << age << " ";
    }
    std::cout << std::endl;

    // keys_view and values_view (for map-like containers)
    std::map<std::string, double> scores = {
        {"Alice", 95.5}, {"Bob", 87.2}, {"Charlie", 92.0}
    };

    auto score_names = scores | std::views::keys;
    std::cout << "Score names: ";
    for (const auto& name : score_names) {
        std::cout << name << " ";
    }
    std::cout << std::endl;

    auto score_values = scores | std::views::values;
    std::cout << "Score values: ";
    for (auto score : score_values) {
        std::cout << score << " ";
    }
    std::cout << std::endl;
}

void testViewComposition() {
    std::cout << "=== Testing View Composition ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};

    // Complex view composition
    auto complex_view = numbers
        | std::views::filter([](int n) { return n % 2 == 1; })  // Only odd numbers
        | std::views::transform([](int n) { return n * n; })     // Square them
        | std::views::drop(2)                                    // Skip first 2
        | std::views::take(4);                                   // Take next 4

    std::cout << "Complex view (odd, squared, skip 2, take 4): ";
    for (auto n : complex_view) {
        std::cout << n << " ";
    }
    std::cout << std::endl;

    // Another composition example
    std::vector<std::string> sentences = {
        "Hello world", "This is a test", "Ranges are powerful",
        "C++ is evolving", "Modern features rock"
    };

    auto word_analysis = sentences
        | std::views::transform([](const std::string& s) {
            return std::ranges::count(s, ' ') + 1;  // Count words
        })
        | std::views::filter([](int word_count) { return word_count >= 3; })
        | std::views::transform([](int count) { return count * 10; });

    std::cout << "Word count analysis (>=3 words, *10): ";
    for (auto count : word_analysis) {
        std::cout << count << " ";
    }
    std::cout << std::endl;
}

void testLazyEvaluation() {
    std::cout << "=== Testing Lazy Evaluation ===" << std::endl;

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

    // Demonstrate lazy evaluation
    auto expensive_transform = [](int n) {
        std::cout << "Processing " << n << " ";
        return n * n * n;  // Cube
    };

    auto lazy_view = numbers
        | std::views::transform(expensive_transform)
        | std::views::take(3);

    std::cout << "Creating lazy view (no processing yet)" << std::endl;

    std::cout << "Now iterating (processing happens): ";
    for (auto n : lazy_view) {
        std::cout << "-> " << n << " ";
    }
    std::cout << std::endl;

    // Demonstrate that views don't store results
    std::cout << "Iterating again (processing happens again): ";
    for (auto n : lazy_view) {
        std::cout << "-> " << n << " ";
    }
    std::cout << std::endl;
}

void testViewsWithAlgorithms() {
    std::cout << "=== Testing Views with Algorithms ===" << std::endl;

    std::vector<double> temperatures = {18.5, 22.1, 19.8, 25.3, 21.7, 23.9, 20.2, 24.6};

    // Create a view and use it with algorithms
    auto comfortable_temps = temperatures
        | std::views::filter([](double temp) { return temp >= 20.0 && temp <= 24.0; });

    std::cout << "Comfortable temperatures (20-24°C): ";
    std::ranges::for_each(comfortable_temps, [](double temp) {
        std::cout << temp << "°C ";
    });
    std::cout << std::endl;

    // Find operations on views
    auto it = std::ranges::find_if(comfortable_temps, [](double temp) {
        return temp > 23.0;
    });

    if (it != comfortable_temps.end()) {
        std::cout << "First comfortable temp > 23°C: " << *it << std::endl;
    }

    // Count with views
    auto count = std::ranges::count_if(comfortable_temps, [](double temp) {
        return static_cast<int>(temp) % 2 == 0;  // Even integer part
    });
    std::cout << "Comfortable temps with even integer part: " << count << std::endl;

    // Accumulate with views (need to convert to concrete container first)
    std::vector<double> comfortable_vec(comfortable_temps.begin(), comfortable_temps.end());
    double avg_temp = std::accumulate(comfortable_vec.begin(), comfortable_vec.end(), 0.0) / comfortable_vec.size();
    std::cout << "Average comfortable temperature: " << avg_temp << "°C" << std::endl;
}

void testCustomViewAdaptors() {
    std::cout << "=== Testing Custom View Adaptors ===" << std::endl;

    // Custom view adaptor for chunking
    auto chunk_by = [](std::size_t chunk_size) {
        return [chunk_size](auto&& range) {
            using Range = std::decay_t<decltype(range)>;
            using Iterator = std::ranges::iterator_t<Range>;

            class ChunkView {
            private:
                Range range_;
                std::size_t chunk_size_;

            public:
                ChunkView(Range&& r, std::size_t size)
                    : range_(std::forward<Range>(r)), chunk_size_(size) {}

                class iterator {
                private:
                    Iterator current_;
                    Iterator end_;
                    std::size_t chunk_size_;

                public:
                    iterator(Iterator current, Iterator end, std::size_t chunk_size)
                        : current_(current), end_(end), chunk_size_(chunk_size) {}

                    std::vector<std::ranges::range_value_t<Range>> operator*() const {
                        std::vector<std::ranges::range_value_t<Range>> chunk;
                        auto it = current_;
                        for (std::size_t i = 0; i < chunk_size_ && it != end_; ++i, ++it) {
                            chunk.push_back(*it);
                        }
                        return chunk;
                    }

                    iterator& operator++() {
                        for (std::size_t i = 0; i < chunk_size_ && current_ != end_; ++i) {
                            ++current_;
                        }
                        return *this;
                    }

                    bool operator!=(const iterator& other) const {
                        return current_ != other.current_;
                    }
                };

                iterator begin() const {
                    return iterator(std::ranges::begin(range_), std::ranges::end(range_), chunk_size_);
                }

                iterator end() const {
                    return iterator(std::ranges::end(range_), std::ranges::end(range_), chunk_size_);
                }
            };

            return ChunkView(std::forward<Range>(range), chunk_size);
        };
    };

    std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12};

    auto chunks = chunk_by(3)(numbers);
    std::cout << "Numbers in chunks of 3:" << std::endl;
    for (const auto& chunk : chunks) {
        std::cout << "  Chunk: ";
        for (auto n : chunk) {
            std::cout << n << " ";
        }
        std::cout << std::endl;
    }
}

void demonstrateViewsAndAdaptors() {
    testBasicViews();
    testAdvancedViews();
    testViewComposition();
    testLazyEvaluation();
    testViewsWithAlgorithms();
    testCustomViewAdaptors();
}
""",
    )

    run_updater(cpp_ranges_project, mock_ingestor)

    project_name = cpp_ranges_project.name

    expected_functions = [
        f"{project_name}.views_adaptors.testBasicViews",
        f"{project_name}.views_adaptors.testAdvancedViews",
        f"{project_name}.views_adaptors.testViewComposition",
        f"{project_name}.views_adaptors.testLazyEvaluation",
        f"{project_name}.views_adaptors.demonstrateViewsAndAdaptors",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_range_pipelines_graph_processing(
    cpp_ranges_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test range pipelines for graph-like data processing scenarios."""
    test_file = cpp_ranges_project / "range_graph_processing.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <ranges>
#include <vector>
#include <map>
#include <set>
#include <string>
#include <iostream>
#include <algorithm>
#include <numeric>
#include <functional>

// Graph node representation
struct GraphNode {
    int id;
    std::string name;
    std::vector<int> connections;
    double weight;

    GraphNode(int i, const std::string& n, std::vector<int> conn, double w)
        : id(i), name(n), connections(std::move(conn)), weight(w) {}
};

// Edge representation
struct Edge {
    int from, to;
    double weight;
    std::string type;

    Edge(int f, int t, double w, const std::string& tp)
        : from(f), to(t), weight(w), type(tp) {}
};

void testGraphNodeProcessing() {
    std::cout << "=== Testing Graph Node Processing with Ranges ===" << std::endl;

    std::vector<GraphNode> nodes = {
        {1, "Node_A", {2, 3, 4}, 10.5},
        {2, "Node_B", {1, 5}, 8.2},
        {3, "Node_C", {1, 4, 6}, 15.1},
        {4, "Node_D", {1, 3}, 12.7},
        {5, "Node_E", {2, 6}, 9.8},
        {6, "Node_F", {3, 5}, 11.3}
    };

    // Find high-weight nodes using ranges
    auto high_weight_nodes = nodes
        | std::views::filter([](const GraphNode& node) { return node.weight > 10.0; })
        | std::views::transform([](const GraphNode& node) { return node.name; });

    std::cout << "High-weight nodes (>10.0): ";
    for (const auto& name : high_weight_nodes) {
        std::cout << name << " ";
    }
    std::cout << std::endl;

    // Find nodes with many connections
    auto well_connected = nodes
        | std::views::filter([](const GraphNode& node) { return node.connections.size() >= 3; })
        | std::views::transform([](const GraphNode& node) {
            return std::make_pair(node.name, node.connections.size());
        });

    std::cout << "Well-connected nodes (>=3 connections):" << std::endl;
    for (const auto& [name, count] : well_connected) {
        std::cout << "  " << name << ": " << count << " connections" << std::endl;
    }

    // Calculate average weight of nodes by connection count
    auto connection_groups = nodes
        | std::views::transform([](const GraphNode& node) {
            return std::make_pair(node.connections.size(), node.weight);
        });

    std::map<std::size_t, std::vector<double>> grouped_weights;
    for (const auto& [conn_count, weight] : connection_groups) {
        grouped_weights[conn_count].push_back(weight);
    }

    std::cout << "Average weights by connection count:" << std::endl;
    for (const auto& [conn_count, weights] : grouped_weights) {
        double avg = std::accumulate(weights.begin(), weights.end(), 0.0) / weights.size();
        std::cout << "  " << conn_count << " connections: avg weight = " << avg << std::endl;
    }
}

void testEdgeProcessing() {
    std::cout << "=== Testing Edge Processing with Ranges ===" << std::endl;

    std::vector<Edge> edges = {
        {1, 2, 5.5, "data_flow"},
        {2, 3, 3.2, "control_flow"},
        {1, 4, 8.1, "data_flow"},
        {3, 5, 2.8, "dependency"},
        {4, 5, 6.4, "data_flow"},
        {5, 6, 4.7, "control_flow"},
        {2, 6, 7.3, "dependency"},
        {1, 3, 1.9, "data_flow"}
    };

    // Group edges by type
    std::map<std::string, std::vector<Edge>> edges_by_type;
    for (const auto& edge : edges) {
        edges_by_type[edge.type].push_back(edge);
    }

    std::cout << "Edges grouped by type:" << std::endl;
    for (const auto& [type, type_edges] : edges_by_type) {
        std::cout << "  " << type << " (" << type_edges.size() << " edges):" << std::endl;

        auto edge_weights = type_edges
            | std::views::transform([](const Edge& e) { return e.weight; });

        double total_weight = std::accumulate(edge_weights.begin(), edge_weights.end(), 0.0);
        double avg_weight = total_weight / type_edges.size();

        std::cout << "    Total weight: " << total_weight
                  << ", Average weight: " << avg_weight << std::endl;
    }

    // Find strongest edges of each type
    std::cout << "Strongest edges by type:" << std::endl;
    for (const auto& [type, type_edges] : edges_by_type) {
        auto strongest = std::ranges::max_element(type_edges, {}, &Edge::weight);
        std::cout << "  " << type << ": " << strongest->from << " -> " << strongest->to
                  << " (weight: " << strongest->weight << ")" << std::endl;
    }

    // Create adjacency analysis
    std::map<int, std::vector<int>> adjacency;
    for (const auto& edge : edges) {
        adjacency[edge.from].push_back(edge.to);
    }

    std::cout << "Node adjacency analysis:" << std::endl;
    for (const auto& [node, neighbors] : adjacency) {
        auto neighbor_view = neighbors | std::views::transform([](int n) { return std::to_string(n); });
        std::cout << "  Node " << node << " -> [";
        bool first = true;
        for (const auto& neighbor_str : neighbor_view) {
            if (!first) std::cout << ", ";
            std::cout << neighbor_str;
            first = false;
        }
        std::cout << "]" << std::endl;
    }
}

void testRangeBasedGraphAlgorithms() {
    std::cout << "=== Testing Range-Based Graph Algorithms ===" << std::endl;

    // Simulate a dependency graph
    std::vector<std::pair<std::string, std::vector<std::string>>> dependencies = {
        {"main.cpp", {"utils.h", "config.h"}},
        {"utils.cpp", {"utils.h"}},
        {"config.cpp", {"config.h", "constants.h"}},
        {"test.cpp", {"main.cpp", "utils.h", "gtest.h"}},
        {"app.cpp", {"main.cpp", "config.h", "ui.h"}}
    };

    // Find files with most dependencies
    auto dependency_counts = dependencies
        | std::views::transform([](const auto& dep) {
            return std::make_pair(dep.first, dep.second.size());
        });

    auto max_deps = std::ranges::max_element(dependency_counts, {},
        [](const auto& p) { return p.second; });

    std::cout << "File with most dependencies: " << max_deps->first
              << " (" << max_deps->second << " dependencies)" << std::endl;

    // Find common dependencies
    std::map<std::string, int> dependency_frequency;
    for (const auto& [file, deps] : dependencies) {
        for (const auto& dep : deps) {
            dependency_frequency[dep]++;
        }
    }

    auto common_deps = dependency_frequency
        | std::views::filter([](const auto& p) { return p.second >= 2; })
        | std::views::transform([](const auto& p) { return p.first; });

    std::cout << "Common dependencies (used by >=2 files): ";
    for (const auto& dep : common_deps) {
        std::cout << dep << " ";
    }
    std::cout << std::endl;

    // Simulate build order analysis
    std::vector<std::string> all_files;
    for (const auto& [file, deps] : dependencies) {
        all_files.push_back(file);
        for (const auto& dep : deps) {
            if (std::ranges::find(all_files, dep) == all_files.end()) {
                all_files.push_back(dep);
            }
        }
    }

    // Categorize files by extension
    auto cpp_files = all_files
        | std::views::filter([](const std::string& file) {
            return file.ends_with(".cpp");
        });

    auto header_files = all_files
        | std::views::filter([](const std::string& file) {
            return file.ends_with(".h");
        });

    std::cout << "C++ source files: ";
    for (const auto& file : cpp_files) {
        std::cout << file << " ";
    }
    std::cout << std::endl;

    std::cout << "Header files: ";
    for (const auto& file : header_files) {
        std::cout << file << " ";
    }
    std::cout << std::endl;
}

void testComplexRangePipelines() {
    std::cout << "=== Testing Complex Range Pipelines ===" << std::endl;

    // Simulate function call graph data
    struct FunctionCall {
        std::string caller;
        std::string callee;
        int frequency;
        double execution_time;
    };

    std::vector<FunctionCall> call_graph = {
        {"main", "initialize", 1, 10.5},
        {"main", "process_data", 1, 250.8},
        {"main", "cleanup", 1, 5.2},
        {"process_data", "load_file", 5, 45.3},
        {"process_data", "parse_content", 5, 120.7},
        {"process_data", "validate_data", 5, 30.1},
        {"parse_content", "tokenize", 20, 15.4},
        {"parse_content", "build_ast", 20, 80.2},
        {"validate_data", "check_syntax", 10, 12.8},
        {"validate_data", "verify_semantics", 10, 25.6}
    };

    // Complex analysis pipeline
    auto performance_analysis = call_graph
        | std::views::filter([](const FunctionCall& call) {
            return call.execution_time > 20.0;  // Focus on slower calls
        })
        | std::views::transform([](const FunctionCall& call) {
            return std::make_tuple(
                call.callee,
                call.frequency * call.execution_time,  // Total time
                call.frequency
            );
        })
        | std::views::filter([](const auto& analysis) {
            return std::get<1>(analysis) > 100.0;  // Total time > 100ms
        });

    std::cout << "Performance hotspots (total time > 100ms):" << std::endl;
    for (const auto& [function, total_time, frequency] : performance_analysis) {
        std::cout << "  " << function << ": " << total_time << "ms total ("
                  << frequency << " calls)" << std::endl;
    }

    // Call hierarchy analysis
    std::map<std::string, std::vector<std::string>> call_hierarchy;
    for (const auto& call : call_graph) {
        call_hierarchy[call.caller].push_back(call.callee);
    }

    std::cout << "Function call hierarchy:" << std::endl;
    for (const auto& [caller, callees] : call_hierarchy) {
        auto callee_analysis = callees
            | std::views::transform([&call_graph](const std::string& callee) {
                auto call_it = std::ranges::find_if(call_graph,
                    [&callee](const FunctionCall& call) {
                        return call.callee == callee;
                    });
                return std::make_pair(callee, call_it != call_graph.end() ? call_it->frequency : 0);
            })
            | std::views::filter([](const auto& p) { return p.second > 1; });

        std::cout << "  " << caller << " frequently calls: ";
        for (const auto& [callee, freq] : callee_analysis) {
            std::cout << callee << "(" << freq << "x) ";
        }
        std::cout << std::endl;
    }
}

void demonstrateRangeGraphProcessing() {
    testGraphNodeProcessing();
    testEdgeProcessing();
    testRangeBasedGraphAlgorithms();
    testComplexRangePipelines();
}
""",
    )

    run_updater(cpp_ranges_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    range_processing_calls = [
        call
        for call in call_relationships
        if "range_graph_processing" in call.args[0][2]
    ]

    assert len(range_processing_calls) >= 5, (
        f"Expected at least 5 range processing calls, found {len(range_processing_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
